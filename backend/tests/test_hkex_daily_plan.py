"""An HKEX daily plan: Source binds each quote to its frozen member, the plan counts it."""

import gzip
import hashlib
from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.canonical import ExchangeDailyQuote
from app.services.full_market import classify_plan, get_plan
from app.services.normalize.exchange_quote import ExchangeDailyQuoteNormalizer
from app.utils import utc_now
from tests.intraday_support import ACCOUNT, RAW_BUCKET
from tests.test_exchange_daily_quote import stub_stores
from tests.test_full_market import DAY, _activated_plan, _source_headers

DATASET = "hk_equity_official_eod"
REPORT = b"<html><pre>DATE: 21 JUL 2026 ... QUOTATIONS ...</pre></html>\n" * 40


def _request(plan, symbol: str, *, currency: str = "HKD", key: str = "hkex-1") -> dict:
    body = gzip.compress(REPORT, mtime=0)
    digest = hashlib.sha256(body).hexdigest()
    return {
        "dataset_key": DATASET,
        "schema_id": "exchange_daily_quote",
        "schema_version": 1,
        "source": "hkex",
        "request_key": key,
        "idempotency_key": key,
        "fetched_at": utc_now().isoformat(),
        "delivery": {
            "slot_id": "asia_pacific_markets_window",
            "scheduled_for": utc_now().isoformat(),
            "target_data_date": DAY.isoformat(),
            "work_item_id": plan.parts[0].work_item_id,
        },
        "payload": {
            "batch": {
                "data_date": DAY.isoformat(),
                "delivery_mode": "incremental",
                "declared_record_count": 1,
                "source_raw_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/hkex/{DATASET}/0123456789abcdef/{digest}.htm.gz",
                "source_raw_sha256": digest,
                "report": {
                    "board": "main",
                    "url": f"https://www.hkex.com.hk/eng/stat/smstat/dayquot/d{DAY:%y%m%d}e.htm",
                    "original_sha256": hashlib.sha256(REPORT).hexdigest(),
                    "original_bytes": len(REPORT),
                    "gzip_bytes": len(body),
                    "last_modified": "2026-07-21T12:00:04Z",
                    "etag": '"etag"',
                },
            },
            "data": [
                {
                    "symbol": symbol,
                    "trade_date": DAY.isoformat(),
                    "name": "CKH HOLDINGS",
                    "currency": currency,
                    "trading_status": "traded",
                    "previous_close": "66.15",
                    "close": "65.15",
                    "ask": "65.15",
                    "bid": "65.10",
                    "high": "66.15",
                    "low": "64.85",
                    "volume": 6614188,
                    "turnover": "431893852",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_a_quote_bound_to_its_member_settles_it_as_data(
    owned_client, test_session, monkeypatch
):
    stores = stub_stores(monkeypatch)
    dataset, body, plan = await _activated_plan(test_session, DATASET, count=2)
    assert body.provider == "hkex"
    headers = await _source_headers(test_session, "hkex", [DATASET])
    await test_session.commit()
    request = _request(plan, "00001")
    payload = request["payload"]
    key = payload["batch"]["source_raw_ref"].split(f"{RAW_BUCKET}/", 1)[1]
    stores.r2.objects[(RAW_BUCKET, key)] = gzip.compress(REPORT, mtime=0)

    accepted = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert accepted.status_code == 202, accepted.text
    run_id = UUID(accepted.json()["run_id"])
    normalizer = ExchangeDailyQuoteNormalizer(
        test_session,
        # The worker passes the raw payload's fetched_at column.
        {
            **dataset.config,
            "_ingest_source": "hkex",
            "_ingest_fetched_at": datetime.fromisoformat(request["fetched_at"]),
        },
    )
    assert (await normalizer.process(payload, run_id)).success_records == 1

    (quote,) = (await test_session.scalars(select(ExchangeDailyQuote))).all()
    assert quote.run_id == run_id
    states = {
        item.member.member_key: item.state for item in await classify_plan(test_session, plan)
    }
    assert states["00001"] == "data" and states["00002"] != "data"
    summary = (
        await get_plan(test_session, plan.plan_id, provider="hkex", allowed_datasets=[DATASET])
    ).summary
    assert summary.data == 1

    # The exchange's counter is the member's currency; another one is not this member.
    other = _request(plan, "00002", currency="USD", key="hkex-2")
    refused = await owned_client.post("/api/v1/source/ingest", headers=headers, json=other)
    assert refused.status_code == 409, refused.text
    assert "currency" in refused.text
