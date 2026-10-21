"""A later canonical upsert must not destroy a queued historical write proof."""

from uuid import UUID

import pytest
from sqlalchemy import select, update

from app.config import get_settings
from app.models.canonical import FuturesContractEOD
from app.models.registry import IngestionRun, SchedulerControl
from app.schemas.historical_backfill import HistoricalResultEvidence
from app.services import historical_backfill, provider_work
from app.utils import uuid7
from tests.test_canonical_ingest_api import _execute_run
from tests.test_taifex_historical import NOW, evidence, seed


@pytest.mark.asyncio
async def test_queued_taifex_completion_survives_later_upsert_with_running_controls(
    client, test_session, test_engine, monkeypatch
):
    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    # seed() pins the backfill clock; the queue must share it or the request
    # ages out of the backfill window once the wall clock passes NOW + 31 days.
    monkeypatch.setattr(provider_work, "utc_now", lambda: NOW)
    headers = await seed(test_session, monkeypatch)
    await test_session.execute(
        update(SchedulerControl)
        .where(SchedulerControl.provider == "taifex")
        .values(desired_state="running", observed_state="running")
    )
    request, _ = await historical_backfill.create_request(
        test_session,
        provider="taifex",
        dataset_key="tw_futures_eod",
        start_date=NOW.date(),
        end_date=NOW.date(),
        request_key="durable-canonical-proof",
        created_by="owner",
    )
    claimed = await provider_work.claim(
        test_session,
        provider="taifex",
        datasets=["tw_futures_eod"],
        host_lock_identity="local:1:22",
    )
    work, _, owner = claimed
    work_id, generation, token = work.work_id, owner.generation, owner.token
    test_session.info["provider_work"] = {
        "work_id": work_id,
        "generation": generation,
        "token": token,
    }
    item = await historical_backfill.claim_next_item(
        test_session, provider="taifex", allowed_datasets=["tw_futures_eod"]
    )
    item_id = item.item_id
    proof = evidence(item_id, request.request_id)
    validated = HistoricalResultEvidence.model_validate(proof)
    await provider_work.freeze_manifest(
        test_session,
        provider="taifex",
        work_id=work_id,
        generation=generation,
        token=token,
        manifest={
            "mode": "canonical",
            "members": [
                {"key": f"{row.product_code}|{row.contract_code}|{row.session}"}
                for row in validated.rows
            ],
            "no_data_evidence": {
                f"{row.product_code}|{row.contract_code}|{row.session}": proof["manifest"]
                for row in validated.rows
                if row.no_trade
            },
        },
    )
    await test_session.commit()
    headers.update(
        {
            "X-FinDB-Work-Id": str(work_id),
            "X-FinDB-Work-Generation": str(generation),
            "X-FinDB-Work-Token": str(token),
            "X-FinDB-Work-Member": f"historical:{item_id}",
        }
    )
    data = [row.model_dump(mode="json") for row in validated.rows if not row.no_trade]
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
    accepted = await client.post("/api/v1/source/ingest", headers=headers, json=body)
    assert accepted.status_code == 202, accepted.text
    run_id = UUID(accepted.json()["run_id"])
    await _execute_run(test_session, test_engine, run_id)
    later = IngestionRun(
        run_id=uuid7(), dataset_key="tw_futures_eod", source="taifex", status="completed"
    )
    test_session.add(later)
    await test_session.flush()
    await test_session.execute(
        update(FuturesContractEOD)
        .where(FuturesContractEOD.run_id == run_id)
        .values(run_id=later.run_id)
    )
    await test_session.commit()
    assert not list(
        await test_session.scalars(
            select(FuturesContractEOD).where(FuturesContractEOD.run_id == run_id)
        )
    )
    complete = await client.post(
        f"/api/v1/source/historical-backfills/{item_id}/complete",
        headers=headers,
        json={
            "status": "completed",
            "lease_token": str(token),
            "run_id": str(run_id),
            "result_evidence": proof,
        },
    )
    assert complete.status_code == 200, complete.text
