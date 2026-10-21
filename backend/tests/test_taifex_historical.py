"""TAIFEX historical HTTP boundaries and canonical whole-day completion proof."""

import copy
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.models.canonical import FuturesContractEOD
from app.models.registry import (
    DatasetRegistry,
    HistoricalBackfillItem,
    IngestionRun,
    SchedulerControl,
    SchedulerDataset,
)
from app.schemas.historical_backfill import HistoricalResultEvidence
from app.services import historical_backfill
from app.services.source_clients import create_source_client
from app.utils import uuid7
from scripts.seed_data import DATASETS
from tests.test_canonical_ingest_api import _execute_run

NOW = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
PRODUCTS = ["TX", "MTX", "TMF", "TE", "TF"]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def reference(raw):
    sha = hashlib.sha256(raw).hexdigest()
    return {
        "ref": f"r2://{'a' * 32}/raw/taifex/tw_futures_eod/123/{sha}.json",
        "sha256": sha,
        "size_bytes": len(raw),
    }


def evidence(item_id, request_id, *, no_data=False):
    rows = []
    for product in PRODUCTS:
        row = {
            "product_code": product,
            "contract_code": f"{product}:202610F1",
            "contract_month": "202610F1",
            "session": "regular",
            "trade_date": "2026-10-01",
            "open": None,
            "high": None,
            "low": None,
            "close": None,
            "volume": 0,
            "settlement_price": None,
            "open_interest": None,
        }
        if not no_data and product != "TF":
            row["close"] = "105"
            row["volume"] = 37
        rows.append(row)
    body = {
        "version": 1,
        "item_id": str(item_id),
        "request_id": str(request_id),
        "trade_date": "2026-10-01",
        "fetched_at": "2026-10-01T10:00:00Z",
        "reports": [
            {
                **reference(product.encode()),
                "product_code": product,
                "url": "https://www.taifex.com.tw/cht/3/futDataDown",
            }
            for product in PRODUCTS
        ],
        "rows": rows,
    }
    return {**body, "manifest": reference(canonical(body))}


async def seed(session, monkeypatch):
    monkeypatch.setattr(historical_backfill, "utc_now", lambda: NOW)
    definition = copy.deepcopy(
        next(item for item in DATASETS if item["dataset_key"] == "tw_futures_eod")
    )
    session.add(DatasetRegistry(**definition))
    for profile in ("pilot", "full_market"):
        key = f"{profile}_taifex_v1"
        session.add(
            SchedulerControl(
                scheduler_key=key,
                provider="taifex",
                execution_profile=profile,
                slot_id="taiwan_market_window",
                timezone="Asia/Taipei",
                desired_state="stopped",
                observed_state="stopped",
            )
        )
        session.add(SchedulerDataset(scheduler_key=key, dataset_key="tw_futures_eod"))
    _, key = await create_source_client(
        session,
        name="taifex-historical",
        source_name="taifex",
        allowed_datasets=["tw_futures_eod"],
        rate_limit_requests=1000,
        rate_limit_window=60,
        commit=False,
    )
    await session.commit()
    return {"X-API-Key": key}


async def claim(session):
    request, _ = await historical_backfill.create_request(
        session,
        provider="taifex",
        dataset_key="tw_futures_eod",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1),
        request_key="taifex-test-request",
        created_by="operator",
    )
    item = await historical_backfill.claim_next_item(
        session, provider="taifex", allowed_datasets=["tw_futures_eod"]
    )
    assert item is not None
    assert item.calendar_snapshot["market"] == "TAIFEX"
    assert item.calendar_snapshot["regular_close"] == "13:45:00"
    await session.commit()
    return item, request


@pytest.mark.asyncio
@pytest.mark.parametrize("no_data", [False, True])
async def test_http_completion_validates_evidence_and_canonical_pipeline(
    client, test_session, test_engine, monkeypatch, no_data
):
    headers = await seed(test_session, monkeypatch)
    item, request = await claim(test_session)
    item_id, request_id, token = item.item_id, request.request_id, item.lease_token
    proof = evidence(item_id, request_id, no_data=no_data)
    HistoricalResultEvidence.model_validate(proof)
    run_id = None
    if not no_data:
        data = proof["rows"][:-1]
        body = {
            "dataset_key": "tw_futures_eod",
            "schema_id": "futures_eod",
            "schema_version": 1,
            "source": "taifex",
            "fetched_at": proof["fetched_at"],
            "request_key": f"historical:{item_id}",
            "idempotency_key": f"historical:{item_id}",
            "delivery": {
                "slot_id": "taiwan_market_window",
                "scheduled_for": proof["fetched_at"],
                "target_data_date": proof["trade_date"],
                "work_item_id": f"historical:{item_id}",
            },
            "payload": {
                "batch": {
                    "data_date": proof["trade_date"],
                    "delivery_mode": "backfill",
                    "coverage_start_date": proof["trade_date"],
                    "coverage_end_date": proof["trade_date"],
                    "declared_record_count": len(data),
                    "source_raw_ref": proof["manifest"]["ref"],
                    "source_raw_sha256": proof["manifest"]["sha256"],
                },
                "data": data,
            },
        }
        response = await client.post("/api/v1/source/ingest", json=body, headers=headers)
        assert response.status_code == 202, response.text
        run_id = UUID(response.json()["run_id"])
        await _execute_run(test_session, test_engine, run_id)
        assert await test_session.scalar(select(func.count()).select_from(FuturesContractEOD)) == 4
        served = await client.get(
            "/api/v1/serve/futures/eod",
            params={"product_code": "TX", "start_date": "2026-10-01", "end_date": "2026-10-01"},
        )
        assert served.status_code == 200, served.text
        assert len(served.json()["data"]) == 1
        repeated = await client.post("/api/v1/source/ingest", json=body, headers=headers)
        assert repeated.status_code == 202 and repeated.json()["run_id"] == str(run_id)
    complete = {
        "status": "completed",
        "lease_token": str(token),
        "run_id": str(run_id) if run_id else None,
    }
    path = f"/api/v1/source/historical-backfills/{item_id}/complete"
    assert (await client.post(path, json=complete, headers=headers)).status_code == 422
    altered = copy.deepcopy(proof)
    altered["request_id"] = str(uuid7())
    altered["manifest"] = reference(
        canonical({key: value for key, value in altered.items() if key != "manifest"})
    )
    assert (
        await client.post(path, json={**complete, "result_evidence": altered}, headers=headers)
    ).status_code == 422
    response = await client.post(path, json={**complete, "result_evidence": proof}, headers=headers)
    assert response.status_code == 200, response.text
    test_session.expire_all()
    saved = await test_session.get(HistoricalBackfillItem, item_id)
    assert saved.status == "completed" and saved.result_evidence == proof
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == (
        0 if no_data else 1
    )

    if not no_data:
        later = NOW + timedelta(minutes=1)
        monkeypatch.setattr(historical_backfill, "utc_now", lambda: later)
        new_request, _ = await historical_backfill.create_request(
            test_session,
            provider="taifex",
            dataset_key="tw_futures_eod",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 1),
            request_key="taifex-official-correction",
            created_by="operator",
        )
        new_item = await historical_backfill.claim_next_item(
            test_session, provider="taifex", allowed_datasets=["tw_futures_eod"]
        )
        assert new_item is not None
        next_id, next_token = new_item.item_id, new_item.lease_token
        corrected = evidence(next_id, new_request.request_id)
        corrected["fetched_at"] = "2026-10-01T10:01:00Z"
        corrected["rows"][0]["close"] = "115"
        corrected["manifest"] = reference(
            canonical({key: value for key, value in corrected.items() if key != "manifest"})
        )
        body.update(
            request_key=f"historical:{next_id}",
            idempotency_key=f"historical:{next_id}",
            fetched_at=corrected["fetched_at"],
        )
        body["delivery"]["work_item_id"] = f"historical:{next_id}"
        body["payload"]["data"] = corrected["rows"][:-1]
        body["payload"]["batch"].update(
            source_raw_ref=corrected["manifest"]["ref"],
            source_raw_sha256=corrected["manifest"]["sha256"],
        )
        response = await client.post("/api/v1/source/ingest", json=body, headers=headers)
        assert response.status_code == 202, response.text
        corrected_run = UUID(response.json()["run_id"])
        await _execute_run(test_session, test_engine, corrected_run)
        response = await client.post(
            f"/api/v1/source/historical-backfills/{next_id}/complete",
            json={
                "status": "completed",
                "lease_token": str(next_token),
                "run_id": str(corrected_run),
                "result_evidence": corrected,
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert await test_session.scalar(select(func.count()).select_from(FuturesContractEOD)) == 4
        assert (
            str(
                await test_session.scalar(
                    select(FuturesContractEOD.close)
                    .where(FuturesContractEOD.run_id == corrected_run)
                    .order_by(FuturesContractEOD.close.desc())
                    .limit(1)
                )
            )
            == "115.00000000"
        )


@pytest.mark.asyncio
async def test_claim_and_authorization_require_stopped_controls_and_1700(
    client, test_session, monkeypatch
):
    headers = await seed(test_session, monkeypatch)
    item, _ = await claim(test_session)
    path = f"/api/v1/source/historical-backfills/{item.item_id}/authorization"
    payload = {"lease_token": str(item.lease_token)}
    assert (await client.post(path, json=payload, headers=headers)).json()["acquisition_allowed"]
    control = await test_session.get(SchedulerControl, "pilot_taifex_v1")
    control.desired_state = "running"
    await test_session.commit()
    assert (await client.post(path, json=payload, headers=headers)).json()[
        "acquisition_allowed"
    ] is False
    assert (
        await historical_backfill.claim_next_item(
            test_session, provider="taifex", allowed_datasets=["tw_futures_eod"]
        )
        is None
    )
    control.desired_state = "stopped"
    await test_session.commit()
    assert historical_backfill._session_closed(item, NOW.replace(hour=9))
    assert not historical_backfill._session_closed(item, NOW.replace(hour=8, minute=59))


@pytest.mark.parametrize(
    "kind", ["missing_product", "duplicate", "date", "manifest", "unknown_null", "wrong_contract"]
)
def test_completion_evidence_rejects_incomplete_or_ambiguous_claims(kind):
    proof = evidence(uuid7(), uuid7(), no_data=True)
    if kind == "missing_product":
        proof["reports"].pop()
    if kind == "duplicate":
        proof["rows"].append(proof["rows"][0])
    if kind == "date":
        proof["rows"][0]["trade_date"] = "2026-09-30"
    if kind == "manifest":
        proof["manifest"]["sha256"] = "0" * 64
    if kind == "unknown_null":
        proof["rows"][0]["volume"] = None
    if kind == "wrong_contract":
        proof["rows"][0]["contract_code"] = "MTX:202610F1"
    with pytest.raises(ValueError):
        HistoricalResultEvidence.model_validate(proof)


@pytest.mark.asyncio
async def test_taifex_window_uses_local_date_and_production_limit(test_session, monkeypatch):
    await seed(test_session, monkeypatch)
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setattr(
        historical_backfill, "utc_now", lambda: datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc)
    )
    oldest = date(2026, 10, 1) - timedelta(days=365)
    _, reason, _ = await historical_backfill.preview_request(
        test_session,
        provider="taifex",
        dataset_key="tw_futures_eod",
        start_date=oldest,
        end_date=date(2026, 10, 1),
    )
    assert reason is None
    _, reason, _ = await historical_backfill.preview_request(
        test_session,
        provider="taifex",
        dataset_key="tw_futures_eod",
        start_date=oldest - timedelta(days=1),
        end_date=date(2026, 10, 1),
    )
    assert reason == "outside_latest_366_days"


@pytest.mark.asyncio
async def test_closed_dates_are_skipped_and_cannot_be_claimed(test_session, monkeypatch):
    await seed(test_session, monkeypatch)
    request, _ = await historical_backfill.create_request(
        test_session,
        provider="taifex",
        dataset_key="tw_futures_eod",
        start_date=date(2026, 9, 26),
        end_date=date(2026, 9, 27),
        request_key="taifex-closed-weekend",
        created_by="operator",
    )
    assert request.status == "completed"
    assert all(item.status == "skipped_calendar" for item in request.items)
    assert (
        await historical_backfill.claim_next_item(
            test_session, provider="taifex", allowed_datasets=["tw_futures_eod"]
        )
        is None
    )
