"""Database barriers verify FIFO ownership and atomic acceptance/closure."""

import asyncio
from datetime import date, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.provider_work import ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import DatasetRegistry, IngestionRun
from app.services import provider_work as queue
from app.services.provider_work import prepared_map
from app.utils import utc_now, uuid7
from tests.provider_work_clock import recent_backfill_day


async def attest_cutover(db, identities=None):
    await queue.apply_cutover(
        db,
        {
            "protocol": "provider-work-v1",
            "environment": queue.environment(),
            "host_id": "test-host",
            "inventory_sha256": "a" * 64,
            "checked_at": queue.utc_now().isoformat(),
            "host_lock_identities": identities
            or {
                provider: "local:1:22"
                for provider in ("factset", "finlab", "shioaji", "taifex", "twelve_data")
            },
        },
    )
    await db.commit()


@pytest.mark.asyncio
async def test_authenticated_admin_single_and_bulk_reruns_wait_for_own_fence_and_audit_lineage(
    owned_client, source_headers, admin_headers, test_session, test_engine, monkeypatch
):
    from app.config import get_settings
    from app.models.provider_work import ProviderWorkReceipt
    from app.models.raw import RawMarketPayload
    from app.models.registry import AdminAuditEvent, NormalizationJob
    from tests.test_canonical_ingest_api import _canonical_request, _execute_run, _seed_dataset

    await _seed_dataset(test_session)
    original = await owned_client.post(
        "/api/v1/source/ingest", headers=source_headers, json=_canonical_request()
    )
    original_id = UUID(original.json()["run_id"])
    await _execute_run(test_session, test_engine, original_id)
    run = await test_session.get(IngestionRun, original_id)
    raw_id = run.raw_payload_id
    raw = await test_session.get(RawMarketPayload, raw_id)
    original_body, original_digest = raw.payload, raw.payload_sha256
    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    from tests.owned_ingest import finish_contract_scope

    await finish_contract_scope(test_session, "finlab")
    await attest_cutover(test_session)
    work = await queue.enqueue(
        test_session,
        provider="finlab",
        stable_key="maintenance:tw_equity_eod:barrier",
        kind="maintenance",
        dataset_key="tw_equity_eod",
        trade_date=None,
    )
    claimed = await queue.claim(
        test_session, provider="finlab", datasets=["tw_equity_eod"], host_lock_identity="local:1:22"
    )
    active_id = work.work_id
    active_fence = identity(claimed)
    await test_session.commit()
    denied = await owned_client.post(f"/api/v1/admin/runs/{original_id}/rerun")
    assert denied.status_code == 401
    single = await owned_client.post(
        f"/api/v1/admin/runs/{original_id}/rerun", headers=admin_headers
    )
    assert single.status_code == 202, single.text
    bulk = await owned_client.post(
        "/api/v1/admin/runs/bulk-rerun?dataset_key=tw_equity_eod&status=completed&limit=1",
        headers=admin_headers,
    )
    assert bulk.status_code == 200 and bulk.json()["queued"] == 1, bulk.text
    reruns = [UUID(single.json()["run_id"]), UUID(bulk.json()["new_run_ids"][0])]
    assert len(set(reruns + [original_id])) == 3
    for new_id in reruns:
        new = await test_session.get(IngestionRun, new_id)
        assert (
            new.raw_payload_id == raw_id
            and new.is_rerun
            and new.metadata_["rerun_from_run_id"] == str(original_id)
        )
        assert (
            await test_session.scalar(
                select(NormalizationJob).where(NormalizationJob.run_id == new_id)
            )
            is None
        )
        receipt = await test_session.get(ProviderWorkReceipt, new_id)
        invalid = await owned_client.post(
            f"/api/v1/source/provider-work/{receipt.work_id}/execute-rerun",
            headers=source_headers,
            json={"generation": active_fence["generation"], "token": str(active_fence["token"])},
        )
        assert invalid.status_code == 409
        audit = await test_session.scalar(
            select(AdminAuditEvent).where(
                AdminAuditEvent.resource_id == str(new_id), AdminAuditEvent.action == "rerun"
            )
        )
        assert (
            audit
            and audit.actor_type != "system"
            and audit.details["rerun_from_run_id"] == str(original_id)
        )
    await queue.close(test_session, **active_fence)
    await queue.settle(test_session, **active_fence, outcome="cancelled", proof={})
    await test_session.commit()
    for new_id in reruns:
        response = await owned_client.post(
            "/api/v1/source/provider-work/claim",
            headers=source_headers,
            json={
                "protocol": "provider-work-v1",
                "host_lock_identity": "local:1:22",
                "recover": True,
            },
        )
        owner = response.json()["work"]
        assert (
            owner["kind"] == "rerun"
            and owner["scope"]["run_id"] == str(new_id)
            and owner["work_id"] != str(active_id)
        )
        fence = {"generation": owner["generation"], "token": owner["token"]}
        execute = await owned_client.post(
            f"/api/v1/source/provider-work/{owner['work_id']}/execute-rerun",
            headers=source_headers,
            json=fence,
        )
        assert execute.status_code == 200
        await _execute_run(test_session, test_engine, new_id)
        await owned_client.post(
            f"/api/v1/source/provider-work/{owner['work_id']}/close",
            headers=source_headers,
            json=fence,
        )
        settled = await owned_client.post(
            f"/api/v1/source/provider-work/{owner['work_id']}/settle",
            headers=source_headers,
            json={**fence, "outcome": "completed", "proof": {}},
        )
        assert settled.status_code == 200, settled.text
    original = await test_session.get(IngestionRun, original_id)
    raw = await test_session.get(RawMarketPayload, raw_id)
    assert original.status == "completed" and original.raw_payload_id == raw_id
    assert raw.payload == original_body and raw.payload_sha256 == original_digest


@pytest.mark.asyncio
async def test_source_daily_close_is_frozen_and_does_not_block_runnable_maintenance(
    client, source_headers, test_session, monkeypatch
):
    from app.config import get_settings
    from app.models.registry import SchedulerControl

    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    dataset = await seed(test_session, "finlab")
    test_session.add(
        SchedulerControl(
            scheduler_key="pilot_finlab_v1",
            provider="finlab",
            execution_profile="pilot",
            desired_state="running",
            observed_state="running",
            timezone="Asia/Taipei",
            slot_id="taiwan_market_window",
        )
    )
    await test_session.commit()
    denied = await client.post(
        "/api/v1/source/provider-work/claim",
        headers=source_headers,
        json={
            "protocol": "provider-work-v1",
            "host_lock_identity": "local:1:22",
            "recover": True,
        },
    )
    assert denied.status_code == 409 and "cutover" in denied.text
    await attest_cutover(test_session)
    body = {
        "kind": "daily",
        "stable_key": f"daily:pilot:{dataset}:2099-10-15",
        "dataset_key": dataset,
        "trade_date": "2099-10-15",
        "execution_profile": "pilot",
        "scope": {"feed": "original"},
        "not_before": "2000-01-01T00:00:00Z",
    }
    first = await client.post(
        "/api/v1/source/provider-work/enqueue", headers=source_headers, json=body
    )
    assert first.status_code == 200, first.text
    attempt = await test_session.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == UUID(first.json()["work_id"])
        )
    )
    assert attempt.not_before.year == 2099 and attempt.not_before.hour == 5

    async def calendar_must_remain_frozen(*args, **kwargs):
        pytest.fail("enqueue replay must retain its original published close")

    monkeypatch.setattr(
        "app.services.effective_calendar.effective_days", calendar_must_remain_frozen
    )
    replay = await client.post(
        "/api/v1/source/provider-work/enqueue", headers=source_headers, json=body
    )
    assert replay.status_code == 200 and replay.json() == first.json()
    ready = await client.post(
        "/api/v1/source/provider-work/enqueue",
        headers=source_headers,
        json={
            "kind": "maintenance",
            "stable_key": f"maintenance:{dataset}:ready",
            "dataset_key": dataset,
        },
    )
    assert ready.status_code == 200
    claim = await client.post(
        "/api/v1/source/provider-work/claim",
        headers=source_headers,
        json={
            "protocol": "provider-work-v1",
            "host_lock_identity": "local:1:22",
            "recover": True,
        },
    )
    assert claim.json()["work"]["work_id"] == ready.json()["work_id"]


@pytest.mark.asyncio
async def test_daily_owner_reports_new_published_closure_without_rewriting_frozen_decision(
    client, source_headers, test_session, monkeypatch
):
    from app.config import get_settings
    from app.models.registry import DailyExecutionDecision
    from tests.test_full_market import DAY, _activated_plan

    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    dataset, body, plan = await _activated_plan(test_session, "tw_equity_eod", count=2)
    work = await queue.enqueue(
        test_session,
        provider=body.provider,
        stable_key=f"daily:full_market:{dataset.dataset_key}:{DAY}",
        kind="daily",
        dataset_key=dataset.dataset_key,
        trade_date=DAY,
        execution_profile="full_market",
        scope={"feed": {"dataset_key": dataset.dataset_key}},
    )
    await attest_cutover(test_session)
    await test_session.commit()
    decision = await test_session.get(
        DailyExecutionDecision,
        (dataset.dataset_key, body.provider, DAY, "full_market"),
    )
    assert decision is not None
    frozen_snapshot = dict(decision.snapshot)
    current_revision_id = str(uuid7())

    async def current_closed(*args, **kwargs):
        return {
            "market": "TW",
            "trade_date": DAY.isoformat(),
            "is_open": False,
            "calendar_source": "published",
            "calendar_revision_id": current_revision_id,
            "policy": {"skip_non_trading_days": False},
        }

    monkeypatch.setattr("app.services.effective_calendar.execution_snapshot", current_closed)
    claimed = await client.post(
        "/api/v1/source/provider-work/claim",
        headers=source_headers,
        json={
            "protocol": "provider-work-v1",
            "host_lock_identity": "local:1:22",
            "recover": True,
        },
    )
    assert claimed.status_code == 200, claimed.text
    owner = claimed.json()["work"]
    assert owner["work_id"] == str(work.work_id)
    assert owner["created_at"] is not None
    status = await client.post(
        f"/api/v1/source/provider-work/{work.work_id}/calendar-status",
        headers=source_headers,
        json={"generation": owner["generation"], "token": owner["token"]},
    )
    assert status.status_code == 200, status.text
    assert status.json()["repair_required"] is True
    assert status.json()["frozen_kind"] == "planned"
    assert (
        status.json()["current_calendar_revision_id"]
        != status.json()["frozen_calendar_revision_id"]
    )
    await test_session.refresh(decision)
    assert decision.kind == "planned"
    assert decision.snapshot == frozen_snapshot
    assert plan.status == "incomplete"

    current_revision_id = frozen_snapshot["calendar_revision_id"]
    unchanged = await client.post(
        f"/api/v1/source/provider-work/{work.work_id}/calendar-status",
        headers=source_headers,
        json={"generation": owner["generation"], "token": owner["token"]},
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["repair_required"] is False


@pytest.mark.asyncio
async def test_enforced_source_receipts_rerun_queue_and_legacy_fail_closed(
    client, source_headers, admin_headers, test_session, test_engine, monkeypatch
):
    from app.config import get_settings
    from app.models.registry import NormalizationJob, SchedulerControl, SchedulerDataset
    from app.services.historical_backfill import create_request
    from tests.test_canonical_ingest_api import _canonical_request, _execute_run, _seed_dataset

    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    await attest_cutover(test_session)
    await _seed_dataset(test_session)
    body = _canonical_request()
    denied = await client.post("/api/v1/source/ingest", json=body, headers=source_headers)
    assert denied.status_code == 409 and denied.json()["error"]["code"] == "PROVIDER_WORK_FENCE"
    test_session.add(
        SchedulerControl(
            scheduler_key="test-history-scope",
            provider="finlab",
            timezone="Asia/Taipei",
            slot_id="taiwan_market_window",
        )
    )
    await test_session.flush()
    test_session.add(
        SchedulerDataset(scheduler_key="test-history-scope", dataset_key="tw_equity_eod")
    )
    await test_session.flush()
    # This fixed July contract exercises acceptance/rerun, within its original window.
    monkeypatch.setattr(
        "app.services.historical_backfill._provider_current_date",
        lambda provider, now: date(2026, 7, 22),
    )
    history, _ = await create_request(
        test_session,
        provider="finlab",
        dataset_key="tw_equity_eod",
        start_date=date(2026, 7, 21),
        end_date=date(2026, 7, 21),
        request_key="source-protocol-test",
        created_by="owner",
        today=date(2026, 7, 21),
    )
    history_item = history.items[0].item_id
    await test_session.commit()
    response = await client.post(
        "/api/v1/source/provider-work/claim",
        headers=source_headers,
        json={"protocol": "provider-work-v1", "host_lock_identity": "local:1:22", "recover": True},
    )
    owner = response.json()["work"]
    work_id = owner["work_id"]
    fence = {"generation": owner["generation"], "token": owner["token"]}
    headers = {
        **source_headers,
        "X-FinDB-Work-Id": work_id,
        "X-FinDB-Work-Generation": str(owner["generation"]),
        "X-FinDB-Work-Token": owner["token"],
        "X-FinDB-Work-Member": body["idempotency_key"],
    }
    leased = await client.post("/api/v1/source/historical-backfills/claim", headers=headers)
    assert leased.status_code == 200, leased.text
    manifest = await client.post(
        f"/api/v1/source/provider-work/{work_id}/manifest",
        headers=source_headers,
        json={**fence, "manifest": {"mode": "canonical", "members": [{"key": "2330"}]}},
    )
    assert manifest.status_code == 200, manifest.text
    accepted = await client.post("/api/v1/source/ingest", json=body, headers=headers)
    assert accepted.status_code == 202, accepted.text
    run_id = UUID(accepted.json()["run_id"])
    rerun = await client.post(f"/api/v1/source/runs/{run_id}/rerun", headers=source_headers)
    assert rerun.status_code == 202, rerun.text
    rerun_id = UUID(rerun.json()["run_id"])
    assert rerun_id != run_id
    assert (
        await test_session.scalar(
            select(NormalizationJob).where(NormalizationJob.run_id == rerun_id)
        )
        is None
    )
    projection = await client.get("/api/v1/admin/provider-work", headers=admin_headers)
    assert projection.status_code == 200 and len(projection.json()["data"]) == 2
    assert "token" not in projection.text and "host_lock_identity" not in projection.text
    await _execute_run(test_session, test_engine, run_id)
    closed = await client.post(
        f"/api/v1/source/provider-work/{work_id}/close", headers=source_headers, json=fence
    )
    assert closed.status_code == 200
    resumed = await client.post("/api/v1/source/historical-backfills/claim", headers=headers)
    assert resumed.status_code == 200 and resumed.json()["item_id"] == str(history_item)
    no_acquisition = await client.post(
        f"/api/v1/source/provider-work/{work_id}/authorize", headers=source_headers, json=fence
    )
    assert no_acquisition.status_code == 409
    settled = await client.post(
        f"/api/v1/source/historical-backfills/{history_item}/complete",
        headers=headers,
        json={"status": "completed", "lease_token": owner["token"], "run_id": str(run_id)},
    )
    assert settled.status_code == 200, settled.text
    replay = await client.post("/api/v1/source/ingest", json=body, headers=headers)
    assert replay.status_code == 202 and replay.json()["run_id"] == str(run_id)
    settled_replay = await client.post(
        f"/api/v1/source/provider-work/{work_id}/settle",
        headers=source_headers,
        json={
            **fence,
            "outcome": "completed",
            "proof": {"historical_item_id": str(history_item), "no_data": []},
        },
    )
    assert settled_replay.status_code == 200
    claimed = await client.post(
        "/api/v1/source/provider-work/claim",
        headers=source_headers,
        json={"protocol": "provider-work-v1", "host_lock_identity": "local:1:22", "recover": True},
    )
    rerun_work = claimed.json()["work"]
    assert rerun_work["kind"] == "rerun"
    rerun_fence = {"generation": rerun_work["generation"], "token": rerun_work["token"]}
    start = await client.post(
        f"/api/v1/source/provider-work/{rerun_work['work_id']}/execute-rerun",
        headers=source_headers,
        json=rerun_fence,
    )
    assert start.status_code == 200, start.text
    assert await test_session.scalar(
        select(NormalizationJob).where(NormalizationJob.run_id == rerun_id)
    )
    assert (await test_session.get(IngestionRun, run_id)).status == "completed"
    closed_rerun = await client.post(
        f"/api/v1/source/provider-work/{rerun_work['work_id']}/close",
        headers=source_headers,
        json=rerun_fence,
    )
    assert closed_rerun.status_code == 200
    waiting = await client.post(
        f"/api/v1/source/provider-work/{rerun_work['work_id']}/settle",
        headers=source_headers,
        json={**rerun_fence, "outcome": "completed", "proof": {}},
    )
    assert (
        waiting.status_code == 409 and waiting.json()["detail"]["code"] == "PROVIDER_WORK_DRAINING"
    )
    # Failed normalization stays failed; completing the coordinator drains the
    # work as failed and leaves both original receipt and new run unchanged.
    from app.schemas.source import RunFailureCode
    from app.services import normalization_queue

    def reject_retained_raw(*args, **kwargs):
        raise normalization_queue.PermanentNormalizationError(
            RunFailureCode.DATASET_CONTRACT_INVALID
        )

    with monkeypatch.context() as failure:
        failure.setattr(normalization_queue, "_select_normalizer_for_payload", reject_retained_raw)
        await _execute_run(test_session, test_engine, rerun_id)
    failed = await test_session.get(IngestionRun, rerun_id)
    assert failed.status == "failed" and failed.failure_code == "DATASET_CONTRACT_INVALID"
    drained = await client.post(
        f"/api/v1/source/provider-work/{rerun_work['work_id']}/settle",
        headers=source_headers,
        json={**rerun_fence, "outcome": "completed", "proof": {}},
    )
    assert drained.status_code == 200 and drained.json()["state"] == "failed"
    lost_ack_replay = await client.post(
        f"/api/v1/source/provider-work/{rerun_work['work_id']}/settle",
        headers=source_headers,
        json={**rerun_fence, "outcome": "completed", "proof": {}},
    )
    assert lost_ack_replay.status_code == 200 and lost_ack_replay.json() == drained.json()
    changed_proof = await client.post(
        f"/api/v1/source/provider-work/{rerun_work['work_id']}/settle",
        headers=source_headers,
        json={**rerun_fence, "outcome": "completed", "proof": {"different": True}},
    )
    assert changed_proof.status_code == 409
    assert (await test_session.get(IngestionRun, run_id)).status == "completed"
    assert (await test_session.get(IngestionRun, rerun_id)).status == "failed"


async def seed(db, provider):
    key = {
        "twelve_data": "us_equity_eod",
        "finlab": "tw_equity_eod",
        "shioaji": "tw_equity_minute",
        "taifex": "tw_futures_eod",
    }[provider]
    if await db.get(DatasetRegistry, key) is None:
        db.add(
            DatasetRegistry(
                dataset_key=key,
                name=key,
                market="US" if provider == "twelve_data" else "TW",
                asset_class="future" if provider == "taifex" else "equity",
            )
        )
        await db.flush()
    return key


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
async def test_enforced_historical_bridge_authorizes_while_both_controls_running(
    client, test_session, monkeypatch, provider
):
    from app.config import get_settings
    from app.models.registry import SchedulerControl, SchedulerDataset
    from app.services.historical_backfill import create_request
    from app.services.source_clients import create_source_client

    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    await attest_cutover(test_session)
    dataset = await seed(test_session, provider)
    for profile in ("pilot", "full_market"):
        key = f"{profile}_{provider}_v1"
        test_session.add(
            SchedulerControl(
                scheduler_key=key,
                provider=provider,
                execution_profile=profile,
                desired_state="running",
                observed_state="running",
                timezone="Asia/Taipei",
                slot_id="taiwan_market_window",
            )
        )
        await test_session.flush()
        test_session.add(SchedulerDataset(scheduler_key=key, dataset_key=dataset))
    await test_session.flush()
    request, _ = await create_request(
        test_session,
        provider=provider,
        dataset_key=dataset,
        start_date=recent_backfill_day(),
        end_date=recent_backfill_day(),
        request_key=f"running-controls-{provider}",
        created_by="owner",
    )
    _, secret = await create_source_client(
        test_session,
        name=f"queue-{provider}",
        source_name=provider,
        allowed_datasets=[dataset],
        rate_limit_requests=1000,
        rate_limit_window=60,
        commit=False,
    )
    await test_session.commit()
    headers = {"X-API-Key": secret}
    legacy = await client.post("/api/v1/source/historical-backfills/claim", headers=headers)
    assert legacy.status_code == 409
    claimed = await client.post(
        "/api/v1/source/provider-work/claim",
        headers=headers,
        json={
            "protocol": "provider-work-v1",
            "host_lock_identity": "local:1:22",
            "accepted_kinds": ["historical"],
            "recover": True,
        },
    )
    work = claimed.json()["work"]
    assert work["stable_key"] == f"historical:{request.items[0].item_id}"
    headers.update(
        {
            "X-FinDB-Work-Id": work["work_id"],
            "X-FinDB-Work-Generation": str(work["generation"]),
            "X-FinDB-Work-Token": work["token"],
        }
    )
    leased = await client.post("/api/v1/source/historical-backfills/claim", headers=headers)
    assert leased.status_code == 200, leased.text
    item_id = leased.json()["item_id"]
    authorization = await client.post(
        f"/api/v1/source/historical-backfills/{item_id}/authorization",
        headers=headers,
        json={"lease_token": work["token"]},
    )
    assert authorization.status_code == 200, authorization.text
    assert authorization.json()["acquisition_allowed"] is True
    fenced = await client.post(
        f"/api/v1/source/provider-work/{work['work_id']}/authorize",
        headers=headers,
        json={"generation": work["generation"], "token": work["token"]},
    )
    assert fenced.status_code == 200, fenced.text
    assert all(
        control.desired_state == "running" and control.observed_state == "running"
        for control in await test_session.scalars(
            select(SchedulerControl).where(SchedulerControl.provider == provider)
        )
    )


async def add(db, provider, key, stable, **kwargs):
    return await queue.enqueue(
        db,
        provider=provider,
        dataset_key=key,
        stable_key=stable,
        kind="maintenance",
        trade_date=date(2026, 10, 15),
        **kwargs,
    )


def identity(claim):
    work, _, owner = claim
    return {
        "provider": work.provider,
        "work_id": work.work_id,
        "generation": owner.generation,
        "token": owner.token,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
async def test_fifo_runnable_retry_tail_and_idempotent_scope(test_session, provider):
    db = test_session
    key = await seed(db, provider)
    first = await add(db, provider, key, "first")
    assert (await add(db, provider, key, "first")).work_id == first.work_id
    with pytest.raises(queue.ProviderWorkError, match="another scope"):
        await add(db, provider, key, "first", scope={"other": True})
    await add(db, provider, key, "future", not_before=utc_now() + timedelta(hours=1))
    second = await add(db, provider, key, "second")
    claimed = await queue.claim(
        db, provider=provider, datasets=[key], host_lock_identity="host:1:22"
    )
    assert claimed[0].work_id == first.work_id
    fence = identity(claimed)
    await queue.close(db, **fence)
    await queue.settle(
        db,
        **fence,
        outcome="retry",
        proof={"reason": "cooldown"},
        retry_at=utc_now() + timedelta(seconds=10),
    )
    attempts = list(
        await db.scalars(select(ProviderWorkAttempt).order_by(ProviderWorkAttempt.queue_order))
    )
    assert [row.attempt_number for row in attempts] == [1, 1, 1, 2]
    assert attempts[-1].work_id == first.work_id
    assert (
        await queue.claim(db, provider=provider, datasets=[key], host_lock_identity="host:1:22")
    )[0].work_id == second.work_id


@pytest.mark.asyncio
async def test_missing_heartbeat_never_releases_and_recovery_fences_old_generation(test_session):
    db = test_session
    key = await seed(db, "taifex")
    first = await add(db, "taifex", key, "first")
    await add(db, "taifex", key, "second")
    claimed = await queue.claim(
        db, provider="taifex", datasets=[key], host_lock_identity="host:1:22"
    )
    old = identity(claimed)
    owner = await db.get(ProviderWorkOwner, (queue.environment(), "taifex"))
    owner.expires_at = utc_now() - timedelta(seconds=1)
    await db.flush()
    assert (
        await queue.claim(db, provider="taifex", datasets=[key], host_lock_identity="host:1:22")
        is None
    )
    assert first.state == "recovery_blocked"
    with pytest.raises(queue.ProviderWorkError, match="shared host lock inode"):
        await queue.claim(
            db, provider="taifex", datasets=[key], host_lock_identity="host:1:23", recover=True
        )
    resumed = await queue.claim(
        db, provider="taifex", datasets=[key], host_lock_identity="host:1:22", recover=True
    )
    assert resumed[0].work_id == first.work_id
    with pytest.raises(queue.ProviderWorkError, match="fence differs"):
        await queue.fence(db, **old, acquisition=True)
    await queue.fence(db, **identity(resumed), acquisition=True)


@pytest.mark.asyncio
async def test_acceptance_transaction_blocks_close_until_receipt_is_committed(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as db:
        key = await seed(db, "finlab")
        work = await add(db, "finlab", key, "acceptance-race")
        claimed = await queue.claim(
            db, provider="finlab", datasets=[key], host_lock_identity="host:1:22"
        )
        owner_id = identity(claimed)
        await queue.freeze_manifest(
            db, **owner_id, manifest={"members": [{"key": "one"}, {"key": "two"}]}
        )
        await db.commit()
    authorized, finish_acceptance, close_started, close_finished = (
        asyncio.Event() for _ in range(4)
    )
    run_id = uuid7()

    async def accept():
        async with sessions() as db:
            db.info["provider_work"] = {**owner_id, "member_key": "one"}
            locked = await queue.acceptance(
                db, provider="finlab", dataset_key=key, trade_date=work.trade_date
            )
            authorized.set()
            await finish_acceptance.wait()
            db.add(IngestionRun(run_id=run_id, dataset_key=key, source="finlab", status="queued"))
            await db.flush()
            await queue.associate_receipt(db, work=locked, run_id=run_id, payload_sha256="a" * 64)
            await db.commit()

    async def closing():
        await authorized.wait()
        async with sessions() as db:
            close_started.set()
            await queue.close(db, **owner_id)
            await db.commit()
            close_finished.set()

    accept_task, close_task = asyncio.create_task(accept()), asyncio.create_task(closing())
    await close_started.wait()
    await asyncio.sleep(0.05)
    assert not close_finished.is_set()
    finish_acceptance.set()
    await asyncio.gather(accept_task, close_task)
    async with sessions() as db:
        with pytest.raises(queue.ProviderWorkError, match="terminal"):
            await queue.settle(db, **owner_id, outcome="completed", proof={})
        run = await db.get(IngestionRun, run_id)
        run.status, run.total_records, run.success_records, run.failed_records = (
            "completed",
            1,
            1,
            0,
        )
        await db.flush()
        with pytest.raises(queue.ProviderWorkError, match="expected scope"):
            await queue.settle(db, **owner_id, outcome="completed", proof={})
        db.info["provider_work"] = {**owner_id, "member_key": "one"}
        locked = await queue.acceptance(
            db, provider="finlab", dataset_key=key, trade_date=work.trade_date
        )
        await queue.associate_receipt(
            db, work=locked, run_id=run_id, payload_sha256="a" * 64, replay=True
        )
        with pytest.raises(queue.ProviderWorkError, match="acceptance is closed"):
            await queue.associate_receipt(db, work=locked, run_id=uuid7(), payload_sha256="a" * 64)


@pytest.mark.asyncio
async def test_same_provider_claim_serialized_while_other_provider_progresses(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as db:
        finlab = await seed(db, "finlab")
        taifex = await seed(db, "taifex")
        await add(db, "finlab", finlab, "first")
        await add(db, "taifex", taifex, "other")
        await db.commit()
    async with sessions() as first, sessions() as second, sessions() as other:
        assert await queue.claim(
            first, provider="finlab", datasets=[finlab], host_lock_identity="host:1:22"
        )
        contender = asyncio.create_task(
            queue.claim(
                second, provider="finlab", datasets=[finlab], host_lock_identity="host:1:22"
            )
        )
        assert await asyncio.wait_for(
            queue.claim(
                other, provider="taifex", datasets=[taifex], host_lock_identity="host:1:23"
            ),
            timeout=1,
        )
        await asyncio.sleep(0.05)
        assert not contender.done()
        await first.commit()
        assert await asyncio.wait_for(contender, timeout=1) is None


@pytest.mark.asyncio
async def test_closed_recovery_replays_exact_manifest_and_rejects_new_acquisition(test_session):
    key = await seed(test_session, "finlab")
    await add(test_session, "finlab", key, "closed-recovery")
    claimed = await queue.claim(
        test_session, provider="finlab", datasets=[key], host_lock_identity="host:1:22"
    )
    manifest = {"members": [{"key": "original"}]}
    await queue.freeze_manifest(test_session, **identity(claimed), manifest=manifest)
    await queue.close(test_session, **identity(claimed))
    await test_session.commit()
    recovered = await queue.claim(
        test_session,
        provider="finlab",
        datasets=[key],
        host_lock_identity="host:1:22",
        recover=True,
    )
    assert recovered[1].state == "closed"
    await queue.freeze_manifest(test_session, **identity(recovered), manifest=manifest)
    with pytest.raises(queue.ProviderWorkError, match="already frozen"):
        await queue.freeze_manifest(
            test_session, **identity(recovered), manifest={"members": [{"key": "new"}]}
        )
    with pytest.raises(queue.ProviderWorkError, match="acquisition is closed"):
        await queue.fence(test_session, **identity(recovered), acquisition=True)


@pytest.mark.asyncio
async def test_unaccepted_original_intent_prevents_terminal_abandon_but_retains_tail_retry(
    test_session,
):
    import json

    from tests.test_canonical_ingest_api import _canonical_request

    key = await seed(test_session, "finlab")
    body = _canonical_request()
    day = date.fromisoformat(body["payload"]["batch"]["data_date"])
    await queue.enqueue(
        test_session,
        provider="finlab",
        stable_key="intent-guard",
        kind="maintenance",
        dataset_key=key,
        trade_date=day,
    )
    claimed = await queue.claim(
        test_session, provider="finlab", datasets=[key], host_lock_identity="host:1:22"
    )
    await queue.freeze_manifest(
        test_session,
        **identity(claimed),
        manifest={
            "mode": "canonical",
            "members": [{"key": row["symbol"]} for row in body["payload"]["data"]],
        },
    )
    await queue.register_prepared(
        test_session, **identity(claimed), body=json.dumps(body), stage="intent", job_key="original"
    )
    await queue.close(test_session, **identity(claimed))
    for outcome in ("completed", "cancelled", "failed"):
        with pytest.raises(queue.ProviderWorkPreparedDrainingError):
            await queue.settle(test_session, **identity(claimed), outcome=outcome, proof={})
    owner = await test_session.get(ProviderWorkOwner, (queue.environment(), "finlab"))
    assert owner.attempt_id == claimed[1].attempt_id
    await queue.settle(
        test_session,
        **identity(claimed),
        outcome="retry",
        retry_at=utc_now() + timedelta(seconds=60),
        proof={},
    )
    assert claimed[0].state == "queued" and (await prepared_map(test_session, claimed[0].work_id))


@pytest.mark.asyncio
async def test_authenticated_subset_claim_waits_for_global_provider_head_and_other_provider_progresses(
    client, test_session, test_engine, monkeypatch
):
    from app.config import get_settings
    from app.dependencies import get_db
    from app.main import app
    from app.services.source_clients import create_source_client

    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    for key, market in (("tw_equity_eod", "TW"), ("tw_etf_eod", "TW"), ("us_equity_eod", "US")):
        test_session.add(
            DatasetRegistry(
                dataset_key=key,
                name=key,
                asset_class="etf" if "etf" in key else "equity",
                market=market,
            )
        )
    await test_session.flush()
    headers = {}
    for name, provider, dataset in (
        ("equity", "finlab", "tw_equity_eod"),
        ("etf", "finlab", "tw_etf_eod"),
        ("us", "twelve_data", "us_equity_eod"),
    ):
        _, secret = await create_source_client(
            test_session,
            name=f"fifo-{name}",
            source_name=provider,
            allowed_datasets=[dataset],
            rate_limit_requests=1000,
            rate_limit_window=60,
            commit=False,
        )
        headers[name] = {"X-API-Key": secret}
    await attest_cutover(test_session)
    work_ids = []
    for provider, dataset, suffix in (
        ("finlab", "tw_equity_eod", "first"),
        ("finlab", "tw_etf_eod", "later"),
        ("twelve_data", "us_equity_eod", "parallel"),
    ):
        work = await queue.enqueue(
            test_session,
            provider=provider,
            dataset_key=dataset,
            stable_key=f"maintenance:{dataset}:{suffix}",
            kind="maintenance",
            trade_date=None,
        )
        work_ids.append(str(work.work_id))
    await test_session.commit()
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)

    async def request_session():
        async with sessions() as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, request_session)
    payload = {"protocol": "provider-work-v1", "host_lock_identity": "local:1:22"}
    denied = await client.post(
        "/api/v1/source/provider-work/claim", headers=headers["etf"], json=payload
    )
    assert denied.status_code == 200 and denied.json() == {"work": None}
    entered, release = asyncio.Event(), asyncio.Event()
    lock = queue.lock_provider
    armed = True

    async def paused_lock(db, provider):
        nonlocal armed
        owner = await lock(db, provider)
        if provider == "finlab" and armed:
            armed = False
            entered.set()
            await release.wait()
        return owner

    monkeypatch.setattr(queue, "lock_provider", paused_lock)
    head = asyncio.create_task(
        client.post("/api/v1/source/provider-work/claim", headers=headers["equity"], json=payload)
    )
    await asyncio.wait_for(entered.wait(), 5)
    subset = asyncio.create_task(
        client.post("/api/v1/source/provider-work/claim", headers=headers["etf"], json=payload)
    )
    try:
        await asyncio.sleep(0.05)
        assert not subset.done()
        other = await client.post(
            "/api/v1/source/provider-work/claim", headers=headers["us"], json=payload
        )
        assert other.status_code == 200 and other.json()["work"]["work_id"] == work_ids[2]
    finally:
        release.set()
    first, blocked = await asyncio.gather(head, subset)
    assert first.status_code == 200 and first.json()["work"]["work_id"] == work_ids[0]
    assert blocked.status_code == 200 and blocked.json() == {"work": None}
    owner = first.json()["work"]
    fence = {"generation": owner["generation"], "token": owner["token"]}
    path = f"/api/v1/source/provider-work/{work_ids[0]}"
    assert (
        await client.post(path + "/close", headers=headers["equity"], json=fence)
    ).status_code == 200
    assert (
        await client.post(
            path + "/settle",
            headers=headers["equity"],
            json={**fence, "outcome": "cancelled", "proof": {}},
        )
    ).status_code == 200
    next = await client.post(
        "/api/v1/source/provider-work/claim", headers=headers["etf"], json=payload
    )
    assert next.status_code == 200 and next.json()["work"]["work_id"] == work_ids[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["source", "admin", "bulk"])
async def test_authenticated_retained_raw_precedence_skip_settles_failed_and_replays_lost_ack(
    owned_client, source_headers, admin_headers, test_session, test_engine, monkeypatch, endpoint
):
    from app.config import get_settings
    from app.models.provider_work import ProviderWork, ProviderWorkReceipt
    from app.models.raw import RawMarketPayload
    from app.models.registry import AdminAuditEvent
    from tests.test_canonical_ingest_api import _canonical_request, _execute_run, _seed_dataset

    await _seed_dataset(test_session)
    response = await owned_client.post(
        "/api/v1/source/ingest", headers=source_headers, json=_canonical_request()
    )
    original_id = UUID(response.json()["run_id"])
    await _execute_run(test_session, test_engine, original_id)
    original = await test_session.get(IngestionRun, original_id)
    raw_id = original.raw_payload_id
    raw = await test_session.get(RawMarketPayload, raw_id)
    frozen_raw = (raw.payload_sha256, dict(raw.payload))
    newer = _canonical_request(idempotency_key="newer-canonical")
    newer["fetched_at"] = "2026-07-21T09:00:00Z"
    response = await owned_client.post("/api/v1/source/ingest", headers=source_headers, json=newer)
    await _execute_run(test_session, test_engine, UUID(response.json()["run_id"]))
    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    from tests.owned_ingest import finish_contract_scope

    await finish_contract_scope(test_session, "finlab")
    await attest_cutover(test_session)
    if endpoint == "bulk":
        response = await owned_client.post(
            "/api/v1/admin/runs/bulk-rerun?dataset_key=tw_equity_eod&status=completed&limit=2",
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        new_ids = [UUID(value) for value in response.json()["new_run_ids"]]
        assert len(new_ids) == 2
    else:
        response = await owned_client.post(
            f"/api/v1/{endpoint}/runs/{original_id}/rerun",
            headers=source_headers if endpoint == "source" else admin_headers,
        )
        assert response.status_code == 202, response.text
        new_ids = [UUID(response.json()["run_id"])]
    old_rerun = None
    for _ in new_ids:
        response = await owned_client.post(
            "/api/v1/source/provider-work/claim",
            headers=source_headers,
            json={"protocol": "provider-work-v1", "host_lock_identity": "local:1:22"},
        )
        assert response.status_code == 200, response.text
        owner = response.json()["work"]
        run_id = UUID(owner["scope"]["run_id"])
        assert run_id in new_ids
        fence = {"generation": owner["generation"], "token": owner["token"]}
        path = f"/api/v1/source/provider-work/{owner['work_id']}"
        assert (
            await owned_client.post(path + "/execute-rerun", headers=source_headers, json=fence)
        ).status_code == 200
        await _execute_run(test_session, test_engine, run_id)
        run = await test_session.get(IngestionRun, run_id)
        skipped = run.metadata_["rerun_from_run_id"] == str(original_id)
        if skipped:
            old_rerun = run_id
            assert (run.status, run.total_records, run.success_records, run.failed_records) == (
                "completed",
                1,
                0,
                0,
            )
            assert run.raw_payload_id == raw_id
        assert (
            await owned_client.post(path + "/close", headers=source_headers, json=fence)
        ).status_code == 200
        request = {**fence, "outcome": "completed", "proof": {}}
        settled = await owned_client.post(path + "/settle", headers=source_headers, json=request)
        assert settled.status_code == 200, settled.text
        replay = await owned_client.post(path + "/settle", headers=source_headers, json=request)
        assert replay.status_code == 200 and replay.json() == settled.json()
        work = await test_session.get(ProviderWork, UUID(owner["work_id"]))
        if skipped:
            assert work.state == "failed"
            round = await test_session.scalar(
                select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work.work_id)
            )
            assert round.failure_code == "NORMALIZATION_FAILED"
            assert (await test_session.get(ProviderWorkReceipt, run_id)).work_id == work.work_id
            if endpoint != "source":
                assert await test_session.scalar(
                    select(AdminAuditEvent.event_id).where(
                        AdminAuditEvent.resource_id == str(run_id),
                        AdminAuditEvent.action == "rerun",
                    )
                )
    assert old_rerun is not None
    test_session.expire_all()
    original = await test_session.get(IngestionRun, original_id)
    raw = await test_session.get(RawMarketPayload, raw_id)
    assert (
        original.status,
        original.total_records,
        original.success_records,
        original.failed_records,
    ) == ("completed", 1, 1, 0)
    assert (raw.payload_sha256, raw.payload) == frozen_raw
    assert (
        await test_session.get(ProviderWorkOwner, (queue.environment(), "finlab"))
    ).attempt_id is None
