"""Each exchange's institutional tables traverse the real durable pipeline to Serve."""

import hashlib
from uuid import UUID

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.registry import NormalizationJob
from app.services.full_market import get_plan
from app.services.normalization_queue import execute_normalization
from app.services.normalize import intraday
from app.utils import utc_now
from tests.intraday_support import ACCOUNT, RAW_BUCKET, Stores
from tests.test_full_market import DAY, _activated_plan, _ingress, _source_headers
from tests.test_institutional_flow import _categories, _rows_object, _security

SHA = "c" * 64


@pytest.fixture
def stores(monkeypatch) -> Stores:
    """The Raw R2 bucket the Worker reads a stock table's rows object from."""
    stores = Stores()
    monkeypatch.setattr(intraday, "raw_reader", lambda: stores.raw)
    return stores


TPEX_SUMMARY = [
    {"category": "foreign_ex_dealer", "buy": 300, "sell": 100},
    {"category": "foreign_dealer", "buy": 0, "sell": 0},
    {"category": "foreign_total", "buy": 300, "sell": 100},
    {"category": "investment_trust", "buy": 40, "sell": 60},
    {"category": "dealer_proprietary", "buy": 10, "sell": 5},
    {"category": "dealer_hedge", "buy": 7, "sell": 9},
    {"category": "dealer_total", "buy": 17, "sell": 14},
    {"category": "total", "buy": 357, "sell": 174},
]


def _row(key: str, board: str, stores) -> dict:
    row = {
        "symbol": board,
        "source_symbol": {
            "tw_twse_institutional_summary": "BFI82U",
            "tw_twse_institutional_stock": "T86",
            "tw_tpex_institutional_summary": "insti_summary",
            "tw_tpex_institutional_stock": "insti_daily_trade",
        }[key],
        "trade_date": DAY.isoformat(),
        "currency": "TWD",
    }
    if key.endswith("_summary"):
        rows = (
            TPEX_SUMMARY
            if board == "TPEX"
            else [
                row
                for row in TPEX_SUMMARY
                if row["category"] not in {"foreign_total", "dealer_total"}
            ]
        )
        return {**row, "categories": _categories(rows)}
    body = _rows_object([_security("2330"), _security("030001")])
    digest = hashlib.sha256(body).hexdigest()
    object_key = f"{board.lower()}/{key}/{digest}.rows.json"
    stores.r2.objects[(RAW_BUCKET, object_key)] = body
    return {
        **row,
        "row_count": 2,
        "rows_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/{object_key}",
        "rows_sha256": digest,
        "rows_bytes": len(body),
        "rows_format": "json",
        "volume_unit": "share",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    "key",
    [
        "tw_twse_institutional_summary",
        "tw_twse_institutional_stock",
        "tw_tpex_institutional_summary",
        "tw_tpex_institutional_stock",
    ],
)
async def test_an_institutional_table_reaches_serve_through_its_daily_plan(
    test_session, test_engine, owned_client, monkeypatch, stores, environment, key
):
    monkeypatch.setattr(get_settings(), "APP_ENVIRONMENT", environment)
    _, body, plan = await _activated_plan(test_session, key, count=1)
    assert [member for part in plan.parts for member in part.member_keys] == [body.provider.upper()]
    headers = await _source_headers(test_session, body.provider, [key])
    await test_session.commit()
    board = body.provider.upper()
    request = _ingress(plan, [_row(key, board, stores)])
    request.update(
        source=body.provider,
        schema_id="institutional_flow_summary"
        if key.endswith("_summary")
        else "institutional_flow_stock",
        schema_version=1,
        request_key=str(plan.plan_id),
        idempotency_key=str(plan.plan_id),
        fetched_at=utc_now().isoformat(),
    )
    request["payload"]["batch"].update(
        source_raw_ref=f"r2://{ACCOUNT}/{RAW_BUCKET}/{body.provider}/x/{SHA}.json",
        source_raw_sha256=SHA,
    )
    accepted = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert accepted.status_code == 202, accepted.text
    run_id = UUID(accepted.json()["run_id"])
    job = (
        await test_session.scalars(
            select(NormalizationJob).where(NormalizationJob.run_id == run_id)
        )
    ).one()
    await execute_normalization(
        run_id, job.delivery_id, database_url=test_engine.url.render_as_string(hide_password=False)
    )
    result = await get_plan(
        test_session, plan.plan_id, provider=body.provider, allowed_datasets=[key]
    )
    assert result.summary.data == result.summary.expected == 1
    assert result.status == "complete"

    if key.endswith("_summary"):
        served = await owned_client.get(
            "/api/v1/serve/institutional-flows/summary",
            params={"start_date": DAY.isoformat(), "end_date": DAY.isoformat()},
        )
    else:
        served = await owned_client.get(
            "/api/v1/serve/institutional-flows", params={"trade_date": DAY.isoformat()}
        )
    assert served.status_code == 200, served.text
    rows = served.json()["data"]
    assert rows and {row["board"] for row in rows} == {board}
    if not key.endswith("_summary"):
        assert {row["symbol"] for row in rows} == {"2330", "030001"}
