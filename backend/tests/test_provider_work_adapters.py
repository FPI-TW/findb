"""Enabled native-to-canonical acceptance for every supported historical scope."""

import asyncio
import hashlib
import json
import time

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkOwner, ProviderWorkReceipt
from app.models.registry import (
    DeliveryMemberEvidence,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
    IngestionRun,
    SchedulerControl,
)
from app.services import provider_work
from app.services.provider_work import prepared_map
from tests.provider_work_runtime_support import SCOPES, RuntimeHarness


@pytest_asyncio.fixture
async def native_runtime(request, client, test_session, test_engine, monkeypatch, tmp_path):
    provider, dataset, *days = request.param
    harness = await RuntimeHarness(
        client,
        test_session,
        test_engine,
        monkeypatch,
        tmp_path,
        provider,
        dataset,
        days[0] if days else 1,
    ).initialize()
    try:
        yield harness
    finally:
        harness.allow_confirm.set()
        harness.allow_terminal.set()
        harness.release_process_lock()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime",
    SCOPES,
    ids=[f"{provider}-{dataset}" for provider, dataset in SCOPES],
    indirect=True,
)
async def test_enabled_historical_native_runtime_completes_every_supported_scope(
    native_runtime, test_engine
):
    h = native_runtime
    assert await h.run()
    await _assert_historical_completed(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", SCOPES, indirect=True)
async def test_historical_ignores_daily_config_outage_and_stopped_daily_controls(
    native_runtime, test_engine, monkeypatch
):
    from findb_fetcher import schedule

    from app.config import get_settings

    h = native_runtime
    controls = list(
        await h.db.scalars(select(SchedulerControl).where(SchedulerControl.provider == h.provider))
    )
    for row in controls:
        row.desired_state = row.observed_state = "stopped"
    await h.db.commit()
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", False)
    monkeypatch.setattr(
        schedule,
        "load_schedule_manifest",
        lambda *args: pytest.fail("historical loaded daily clock"),
    )
    handle = h.handle
    schedule_reads = []

    def unavailable(request):
        if request.url.path == "/api/v1/source/execution-config":
            import httpx

            schedule_reads.append(request)
            return httpx.Response(503, request=request)
        return handle(request)

    h.handle = unavailable
    assert await h.run()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    assert work.state == "completed", (h.source_events, h.failure)
    assert (await h.db.get(HistoricalBackfillItem, h.item_id)).status == "completed"
    assert h.native_calls() and h.runs and not schedule_reads
    assert all(
        row.desired_state == "stopped"
        for row in await h.db.scalars(
            select(SchedulerControl).where(SchedulerControl.provider == h.provider)
        )
    )


async def _assert_historical_completed(h):
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    item = await h.db.get(HistoricalBackfillItem, h.item_id)
    assert work.state == item.status == "completed", (h.native_calls(), h.source_events, h.failure)
    assert h.native_calls() and h.runs
    receipts = list(
        await h.db.scalars(
            select(ProviderWorkReceipt).where(ProviderWorkReceipt.work_id == work.work_id)
        )
    )
    assert {row.run_id for row in receipts} == set(h.runs)
    evidence = set(
        await h.db.scalars(
            select(DeliveryMemberEvidence.member_key).where(
                DeliveryMemberEvidence.run_id.in_(h.runs)
            )
        )
    )
    assert evidence == {row["key"] for row in work.manifest["members"]}
    assert all(
        row.status == "completed" and row.failed_records == 0
        for row in await h.db.scalars(select(IngestionRun).where(IngestionRun.run_id.in_(h.runs)))
    )
    assert all(
        row.desired_state == "running"
        for row in await h.db.scalars(
            select(SchedulerControl).where(SchedulerControl.provider == h.provider)
        )
    )
    assert (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime",
    [("finlab", "tw_equity_eod"), ("taifex", "tw_futures_eod")],
    indirect=True,
)
async def test_cancel_after_local_prepared_save_before_confirmation_drains_exact_bytes_and_blocks_next_owner(
    native_runtime, admin_headers, test_engine
):
    h = native_runtime
    h.confirm_barrier, h.hold_terminal = True, True
    task = asyncio.create_task(h.run())
    await asyncio.wait_for(h.prepared.wait(), 15)
    calls = h.native_calls()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    descriptor = next(iter((await prepared_map(h.db, work.work_id)).values()))
    work_id = work.work_id
    assert (
        not descriptor["confirmed"]
        and descriptor["body_sha256"] == hashlib.sha256(h.saved_body).hexdigest()
    )
    assert not h.runs
    cancelled = await h.client.post(
        f"/api/v1/admin/historical-backfills/{h.request_id}/cancel", headers=admin_headers
    )
    assert cancelled.status_code == 200, cancelled.text
    next_work = await provider_work.enqueue(
        h.db,
        provider=h.provider,
        stable_key=f"maintenance:{h.dataset}:next",
        kind="maintenance",
        dataset_key=h.dataset,
        trade_date=None,
    )
    next_id = next_work.work_id
    await h.db.commit()
    body = json.loads(h.saved_body)
    if body["schema_id"] == "market_minute":
        from datetime import date

        from app.schemas.ingress import minute_sequence_key_digest
        from app.utils import uuid7

        batch = body["payload"]["batch"]
        batch["snapshot_id"] = str(uuid7())
        digest = minute_sequence_key_digest(
            dataset_key=h.dataset,
            data_date=date.fromisoformat(batch["data_date"]),
            snapshot_id=batch["snapshot_id"],
            sequence=batch["sequence"],
        )
        body["request_key"], body["idempotency_key"] = f"mmr:{digest}", f"mms:{digest}"
    else:
        body["idempotency_key"] = "not-prepared"
    owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    generation, token, inode = owner.generation, str(owner.token), owner.host_lock_identity
    headers = {
        **h.headers,
        "X-FinDB-Work-Id": str(work_id),
        "X-FinDB-Work-Generation": str(generation),
        "X-FinDB-Work-Token": token,
        "X-FinDB-Work-Member": body["idempotency_key"],
    }
    rejected = await h.client.post("/api/v1/source/ingest", headers=headers, json=body)
    assert rejected.status_code == 409
    invented_intent = await h.client.post(
        f"/api/v1/source/provider-work/{work_id}/prepare",
        headers=h.headers,
        json={
            "generation": generation,
            "token": token,
            "body": json.dumps(body),
            "stage": "intent",
        },
    )
    assert invented_intent.status_code == 409
    h.allow_confirm.set()
    await asyncio.wait_for(h.accepted.wait(), 15)
    with pytest.raises(h.queue.ProviderWorkWaitingError):
        with h.queue.host_lock(h.provider):
            pytest.fail("accepted normalization has not drained")
    assert (
        await provider_work.claim(
            h.db,
            provider=h.provider,
            datasets=[h.dataset],
            host_lock_identity=inode,
            recover=False,
        )
        is None
    )
    await h.db.commit()
    assert h.native_calls() == calls
    await h.normalize()
    h.hold_terminal = False
    h.allow_terminal.set()
    assert await asyncio.wait_for(task, 15)
    h.db.expire_all()
    assert (await h.db.get(ProviderWork, work_id)).state == "cancelled"
    assert (await h.db.get(HistoricalBackfillItem, h.item_id)).status == "cancelled"
    assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "cancelled"
    assert h.native_calls() == calls
    assert h.runs and all(
        row.status == "completed"
        for row in await h.db.scalars(select(IngestionRun).where(IngestionRun.run_id.in_(h.runs)))
    )
    next_owner = await provider_work.claim(
        h.db, provider=h.provider, datasets=[h.dataset], host_lock_identity=inode
    )
    assert next_owner[0].work_id == next_id


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("finlab", "tw_equity_eod")], indirect=True)
@pytest.mark.parametrize("window", ["intent_commit_ack_lost", "local_save_crash_before_confirm"])
async def test_committed_intent_lost_ack_recovers_original_body_after_cancel_without_refetch(
    native_runtime, admin_headers, test_engine, window
):
    h = native_runtime
    h.drop_intent_ack = window == "intent_commit_ack_lost"
    h.crash_after_local_save = window == "local_save_crash_before_confirm"
    with pytest.raises(h.queue.ProviderWorkRecoveryBlockedError):
        await h.run()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    original = next(iter((await prepared_map(h.db, work.work_id)).values()))["body"]
    work_id = work.work_id
    calls = h.native_calls()
    assert not h.runs
    cancelled = await h.client.post(
        f"/api/v1/admin/historical-backfills/{h.request_id}/cancel", headers=admin_headers
    )
    assert cancelled.status_code == 200
    h.release_process_lock()
    assert await h.run()
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert (
        work.state == "cancelled"
        and next(iter((await prepared_map(h.db, work.work_id)).values()))["body"] == original
    )
    assert h.runs and h.native_calls() == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("finlab", "tw_equity_eod")], indirect=True)
async def test_preparation_intent_transaction_serializes_cancel_before_local_save(
    native_runtime, test_engine, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.historical_backfill import cancel_request

    h = native_runtime
    h.confirm_barrier, h.hold_terminal = True, True
    intent_written, allow_commit, cancel_started = (asyncio.Event() for _ in range(3))
    register = provider_work.register_prepared

    async def pause_before_commit(db, **kwargs):
        await register(db, **kwargs)
        if kwargs["stage"] == "intent":
            intent_written.set()
            await allow_commit.wait()

    monkeypatch.setattr(provider_work, "register_prepared", pause_before_commit)
    runtime = asyncio.create_task(h.run())
    await asyncio.wait_for(intent_written.wait(), 15)
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)

    async def cancel():
        async with sessions() as db:
            cancel_started.set()
            await cancel_request(db, h.request_id, actor="transaction-barrier-owner")
            await db.commit()

    cancellation = asyncio.create_task(cancel())
    try:
        await cancel_started.wait()
        await asyncio.sleep(0.05)
        assert not cancellation.done(), "cancel must wait for the exact preparation intent commit"
        assert not h.prepared.is_set() and not h.runs
        allow_commit.set()
        await asyncio.wait_for(cancellation, 15)
        await asyncio.wait_for(h.prepared.wait(), 15)
        calls = h.native_calls()
        h.allow_confirm.set()
        await asyncio.wait_for(h.accepted.wait(), 15)
        await h.normalize()
        h.hold_terminal = False
        h.allow_terminal.set()
        assert await asyncio.wait_for(runtime, 15)
        h.db.expire_all()
        assert (await h.db.get(HistoricalBackfillItem, h.item_id)).status == "cancelled"
        assert h.runs and h.native_calls() == calls
    finally:
        allow_commit.set()
        h.allow_confirm.set()
        h.allow_terminal.set()
        await asyncio.gather(runtime, cancellation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime",
    [("finlab", "tw_equity_eod"), ("twelve_data", "us_equity_eod")],
    indirect=True,
)
async def test_historical_restart_after_close_commit_replays_frozen_scope_without_provider_io(
    native_runtime, test_engine
):
    import httpx

    from app.models.provider_work import ProviderWorkAttempt

    h = native_runtime
    handle = h.handle
    rejected = False
    crashed = False

    def close_crash(request):
        nonlocal rejected, crashed
        if request.url.path == "/api/v1/source/ingest" and not rejected:
            rejected = True
            return httpx.Response(
                503, json={"detail": "temporary Source rejection"}, request=request
            )
        response = handle(request)
        if request.url.path.endswith("/close") and not crashed and response.status_code == 200:
            crashed = True
            raise h.queue.ProviderWorkRecoveryBlockedError(
                "simulated process death after close commit"
            )
        return response

    h.handle = close_crash
    with pytest.raises(h.queue.ProviderWorkRecoveryBlockedError):
        await h.run()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    work_id, frozen, bodies = (
        work.work_id,
        work.manifest,
        dict((await prepared_map(h.db, work.work_id))),
    )
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work_id)
    )
    assert crashed and attempt.state == "closed"
    calls = h.native_calls()
    h.release_process_lock()
    h.handle = handle
    assert await h.run()
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.manifest == frozen and h.native_calls() == calls
    if work.state == "queued":
        from datetime import timedelta

        from app.utils import utc_now

        retry = await h.db.scalar(
            select(ProviderWorkAttempt).where(
                ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
            )
        )
        retry.not_before = utc_now() - timedelta(seconds=1)
        await h.db.commit()
        assert await h.run()
        h.db.expire_all()
        work = await h.db.get(ProviderWork, work_id)
    assert work.state == "completed", h.failure
    saved = await prepared_map(h.db, work.work_id)
    assert all(saved[key]["body"] == row["body"] for key, row in bodies.items())


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", SCOPES, indirect=True)
async def test_permanent_prepared_rejection_retains_body_and_explicit_manual_failure(
    native_runtime, test_engine
):
    from app.models.provider_work import ProviderWorkAttempt
    from app.models.registry import DatasetRegistry

    h = native_runtime
    handle = h.handle
    disabled = False

    def deactivate_after_prepare(request):
        nonlocal disabled
        if request.url.path == "/api/v1/source/ingest" and not disabled:
            disabled = True

            async def deactivate():
                dataset = await h.db.get(DatasetRegistry, h.dataset)
                dataset.is_active = False
                await h.db.commit()

            asyncio.run_coroutine_threadsafe(deactivate(), h.loop).result(timeout=15)
        return handle(request)

    h.handle = deactivate_after_prepare
    assert await h.run()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work.work_id)
    )
    assert work.state == "failed" and (await prepared_map(h.db, work.work_id))
    assert attempt.failure_code == "PREPARED_DELIVERY_MANUAL_REVIEW"
    error = json.loads(attempt.proof["source_rejection"])
    assert error["http_status"] == 409 and error["code"] == "DATASET_INACTIVE"
    assert error["delivery_key"] in (await prepared_map(h.db, work.work_id))
    assert (
        error["body_sha256"]
        == (await prepared_map(h.db, work.work_id))[error["delivery_key"]]["body_sha256"]
    )
    assert set(attempt.proof["manual_pending_prepared"]) == set(
        (await prepared_map(h.db, work.work_id))
    )
    assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "failed"
    assert not h.runs
    owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    assert owner.attempt_id is None
    following = await provider_work.enqueue(
        h.db,
        provider=h.provider,
        stable_key=f"after-manual:{h.item_id}",
        kind="maintenance",
        dataset_key=h.dataset,
        trade_date=None,
        scope={"reason": "following-runnable-work"},
    )
    claimed = await provider_work.claim(
        h.db,
        provider=h.provider,
        datasets=[h.dataset],
        host_lock_identity=owner.host_lock_identity,
        require_cutover=True,
    )
    assert claimed is not None and claimed[0].work_id == following.work_id
    identity = {
        "provider": h.provider,
        "work_id": following.work_id,
        "generation": claimed[2].generation,
        "token": claimed[2].token,
    }
    await provider_work.close(h.db, **identity)
    await provider_work.settle(h.db, outcome="cancelled", proof={}, **identity)
    await h.db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime,fence_delay",
    [(scope, 0) for scope in SCOPES] + [(("taifex", "tw_futures_eod"), 0.06)],
    indirect=["native_runtime"],
)
@pytest.mark.parametrize("temporary_status", [408, 503])
async def test_cancel_queued_prepared_retry_preserves_executable_drain_without_new_provider_io(
    native_runtime, test_engine, admin_headers, temporary_status, fence_delay
):
    from datetime import timedelta

    import httpx

    from app.models.provider_work import ProviderWorkAttempt
    from app.utils import utc_now

    h = native_runtime
    handle = h.handle
    delayed_checks = []

    def slow_authorization(request):
        response = handle(request)
        if fence_delay and request.url.path.endswith("/authorize"):
            assert response.status_code == 200
            time.sleep(fence_delay)
            delayed_checks.append(request.url.path)
        return response

    def unavailable(request):
        if request.url.path == "/api/v1/source/ingest":
            return httpx.Response(
                temporary_status, json={"detail": "temporarily unavailable"}, request=request
            )
        return slow_authorization(request)

    h.handle = unavailable
    assert await h.run()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    work_id, frozen, original = (
        work.work_id,
        work.manifest,
        dict((await prepared_map(h.db, work.work_id))),
    )
    assert work.state == "queued" and original and not h.runs, (h.failure, h.source_events)
    if fence_delay:
        assert delayed_checks, "the prepared retry must survive actual slow work authorization"
    retry = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    retry_id, retry_at = retry.attempt_id, retry.not_before
    calls = h.native_calls()
    response = await h.client.post(
        f"/api/v1/admin/historical-backfills/{h.request_id}/cancel", headers=admin_headers
    )
    assert response.status_code == 200, response.text
    h.db.expire_all()
    work, retry = (
        await h.db.get(ProviderWork, work_id),
        await h.db.get(ProviderWorkAttempt, retry_id),
    )
    assert work.state == "queued" and retry.state == "queued" and retry.drain_only
    assert retry.not_before == retry_at and work.manifest == frozen
    assert (await h.db.get(HistoricalBackfillItem, h.item_id)).status == "cancelled"
    from tests.test_provider_work import attest_cutover

    identities = {
        owner.provider: owner.host_lock_identity
        for owner in await h.db.scalars(select(ProviderWorkOwner))
    }
    await attest_cutover(h.db, identities)
    await h.db.refresh(work)
    assert work.state == "queued", (
        "repeated cutover reconciliation must retain the same continuation"
    )
    retry.not_before = utc_now() - timedelta(seconds=1)
    await h.db.commit()
    h.handle = handle
    assert await h.run()
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "cancelled" and h.native_calls() == calls
    assert work.manifest == frozen and (await prepared_map(h.db, work.work_id)) == {
        key: {**value, "confirmed": True} for key, value in original.items()
    }
    assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "cancelled"
    assert (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id is None
    assert set(
        await h.db.scalars(
            select(ProviderWorkReceipt.member_key).where(ProviderWorkReceipt.work_id == work_id)
        )
    ) == set(original)
