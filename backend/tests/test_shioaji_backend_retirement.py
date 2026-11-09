"""Retirement closes new admission without rewriting retained workflow evidence."""

import runpy
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models.provider_work import (
    ProviderWork,
    ProviderWorkAttempt,
    ProviderWorkOwner,
    ProviderWorkPrepared,
    ProviderWorkReceipt,
)
from app.models.registry import (
    DatasetRegistry,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
    IngestionRun,
    SchedulerControl,
    SchedulerDataset,
)
from app.services import historical_backfill as history
from app.services import provider_work as queue
from app.utils import utc_now, uuid7


def _proof():
    return {
        "environment": queue.environment(),
        "checked_at": utc_now().isoformat(),
        "host_lock_identities": {
            provider: f"{queue.environment()}:1:{index}"
            for index, provider in enumerate(("factset", "finlab", "taifex", "twelve_data"), 1)
        },
    }


async def _retained_history(db, dataset_key="tw_equity_minute", status="failed"):
    """Persist a pre-retirement request directly; never reopen runtime capability."""
    day = history._provider_current_date("shioaji", utc_now()) - timedelta(days=1)
    if await db.get(DatasetRegistry, dataset_key) is None:
        db.add(
            DatasetRegistry(
                dataset_key=dataset_key, name=dataset_key, market="TW", asset_class="equity"
            )
        )
    db.add(
        SchedulerControl(
            scheduler_key="old-shioaji",
            provider="shioaji",
            slot_id="taiwan_market_window",
            timezone="Asia/Taipei",
        )
    )
    await db.flush()
    db.add(SchedulerDataset(scheduler_key="old-shioaji", dataset_key=dataset_key))
    request = HistoricalBackfillRequest(
        request_id=uuid7(),
        request_key="retained-shioaji",
        provider="shioaji",
        dataset_key=dataset_key,
        market="TW",
        start_date=day,
        end_date=day,
        created_by="original-owner",
        status=status,
    )
    item = HistoricalBackfillItem(
        item_id=uuid7(),
        trade_date=day,
        status="pending" if status == "queued" else status,
        lease_attempt=3,
        calendar_snapshot={
            "trade_date": day.isoformat(),
            "timezone": "Asia/Taipei",
            "regular_close": "13:30:00",
        },
    )
    request.items = [item]
    db.add(request)
    await db.flush()
    work = ProviderWork(
        work_id=uuid7(),
        environment=queue.environment(),
        provider="shioaji",
        stable_key=f"historical:{item.item_id}",
        kind="historical",
        dataset_key=dataset_key,
        trade_date=day,
        scope={"item_id": str(item.item_id), "request_id": str(request.request_id)},
        manifest={"retained": "original frozen evidence"},
        state="queued",
    )
    db.add(work)
    await db.flush()
    attempt = ProviderWorkAttempt(work_id=work.work_id, attempt_number=3, state="queued")
    run = IngestionRun(
        dataset_key=dataset_key,
        source="shioaji",
        schema_id="market_minute",
        schema_version=1,
        status="completed",
    )
    db.add_all([attempt, run])
    await db.flush()
    owner = ProviderWorkOwner(
        environment=queue.environment(),
        provider="shioaji",
        generation=7,
        host_lock_identity=f"{queue.environment()}:9:99",
        recovery_reason="retained evidence",
    )
    prepared = ProviderWorkPrepared(
        work_id=work.work_id,
        delivery_key="old-sequence",
        descriptor={"body_sha256": "a" * 64},
        body='{"retained":true}',
    )
    receipt = ProviderWorkReceipt(
        run_id=run.run_id,
        work_id=work.work_id,
        attempt_id=attempt.attempt_id,
        member_key="old-sequence",
        payload_sha256="a" * 64,
    )
    db.add_all([owner, prepared, receipt])
    await db.flush()
    return request, item, work, attempt, run, owner, prepared, receipt


async def _snapshot(db, records):
    result = []
    for row in records:
        await db.refresh(row)
        result.append({column.key: getattr(row, column.key) for column in row.__table__.columns})
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["local", "staging", "production"])
@pytest.mark.parametrize("dataset_key", ["tw_equity_minute", "tw_etf_minute"])
async def test_shioaji_new_requests_are_rejected_but_history_remains_readable(
    client, test_session, admin_headers, monkeypatch, environment, dataset_key
):
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    records = await _retained_history(test_session, dataset_key)
    request = records[0]
    before = await _snapshot(test_session, records)
    scopes = await client.get("/api/v1/admin/historical-backfills/scopes", headers=admin_headers)
    assert scopes.status_code == 200
    assert all(row["provider"] != "shioaji" for row in scopes.json()["data"])
    body = {
        "provider": "shioaji",
        "dataset_key": dataset_key,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "request_key": "new-shioaji-request",
    }
    preview = await client.post(
        "/api/v1/admin/historical-backfills/preview", headers=admin_headers, json=body
    )
    assert preview.status_code == 200
    assert preview.json()["scope_reason"] == "provider_dataset_unsupported"
    assert preview.json()["scope_valid"] is False
    assert all(not day["valid"] for day in preview.json()["days"])
    denied = await client.post(
        "/api/v1/admin/historical-backfills", headers=admin_headers, json=body
    )
    assert denied.status_code == 422 and "does not support" in denied.text
    listed = await client.get("/api/v1/admin/historical-backfills", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()["pagination"]["total_records"] == 1
    saved = listed.json()["data"][0]
    assert saved["request_id"] == str(request.request_id) and saved["status"] == "failed"
    assert saved["items"][0]["item_id"] == str(records[1].item_id)
    # Idempotent replay references the old request and cannot reopen or enqueue it.
    replay = await client.post(
        "/api/v1/admin/historical-backfills",
        headers=admin_headers,
        json={**body, "request_key": request.request_key},
    )
    assert replay.status_code == 202 and replay.json()["request_id"] == str(request.request_id)
    assert await _snapshot(test_session, records) == before
    assert await test_session.scalar(select(func.count()).select_from(ProviderWork)) == 1
    assert await test_session.scalar(select(func.count()).select_from(ProviderWorkAttempt)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "queued"])
async def test_cutover_uses_four_providers_and_preserves_retired_history(test_session, status):
    records = await _retained_history(test_session, status=status)
    before = await _snapshot(test_session, records)
    proof = _proof()
    for _ in range(2):
        await queue.apply_cutover(test_session, proof)
        await test_session.flush()
        assert await _snapshot(test_session, records) == before
    for provider, inode in proof["host_lock_identities"].items():
        owner = await test_session.get(ProviderWorkOwner, (queue.environment(), provider))
        assert owner.host_lock_identity == inode


@pytest.mark.asyncio
async def test_cutover_does_not_create_retired_owner_and_matches_host_preflight(test_session):
    host = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "infra/deploy/provider_work_preflight.py")
    )
    assert set(_proof()["host_lock_identities"]) == set(host["PROVIDERS"])
    await queue.apply_cutover(test_session, _proof())
    assert await test_session.get(ProviderWorkOwner, (queue.environment(), "shioaji")) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "retired", "stale", "environment"])
async def test_cutover_rejects_incomplete_or_old_inventory_before_writes(test_session, invalid):
    proof = _proof()
    if invalid == "missing":
        del proof["host_lock_identities"]["factset"]
    elif invalid == "retired":
        proof["host_lock_identities"]["shioaji"] = f"{queue.environment()}:9:99"
    elif invalid == "stale":
        proof["checked_at"] = (utc_now() - timedelta(minutes=6)).isoformat()
    else:
        proof["environment"] = "other"
    with pytest.raises(queue.ProviderWorkError, match="inventory"):
        await queue.apply_cutover(test_session, proof)
    assert await test_session.scalar(select(func.count()).select_from(ProviderWorkOwner)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["owner", "normalization", "historical"])
async def test_cutover_still_requires_retired_inflight_work_to_drain(test_session, blocker):
    records = await _retained_history(test_session)
    request, item, _, attempt, run, owner, *_ = records
    if blocker == "owner":
        owner.attempt_id = attempt.attempt_id
    elif blocker == "normalization":
        run.status = "pending"
    else:
        request.status, item.status = "running", "running"
    await test_session.flush()
    before = await _snapshot(test_session, records)
    with pytest.raises(queue.ProviderWorkError):
        async with test_session.begin_nested():
            await queue.apply_cutover(test_session, _proof())
    assert await _snapshot(test_session, records) == before


@pytest.mark.asyncio
async def test_retained_request_can_still_be_cancelled_without_discarding_prepared(test_session):
    records = await _retained_history(test_session, status="queued")
    request, item, work, attempt, _, _, prepared, receipt = records
    retained = await _snapshot(test_session, [prepared, receipt])
    await history.cancel_request(test_session, request.request_id, actor="owner")
    assert request.status == item.status == "cancelled"
    assert work.state == attempt.state == "queued" and attempt.drain_only
    assert await _snapshot(test_session, [prepared, receipt]) == retained
