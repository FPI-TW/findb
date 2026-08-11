"""DB-authoritative normalization queue and reconciliation tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from celery.exceptions import Reject, Retry
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import reset_source_rate_limit_state
from app.config import get_settings
from app.dependencies import get_db
from app.main import app
from app.models.raw import RawMarketPayload
from app.models.registry import (
    DatasetRegistry,
    IngestionRun,
    NormalizationJob,
    NormalizationOutbox,
    SourceClient,
)
from app.services.normalization_queue import (
    _defer_for_lock_contention,
    reconcile_nonterminal_jobs,
    reconcile_stale_jobs,
)
from app.task_queue import normalization_queue
from app.utils import utc_now, uuid7
from app.workers.tasks import normalize_run
from scripts.cleanup_raw import cleanup_expired_raw


def test_queue_consumer_timeout_exceeds_task_hard_limit():
    settings = get_settings()
    assert "x-consumer-timeout" not in normalization_queue.queue_arguments
    assert settings.NORMALIZATION_CONSUMER_TIMEOUT_MS > (
        settings.NORMALIZATION_TASK_TIME_LIMIT * 1000
    )


def test_worker_infrastructure_failure_uses_delayed_retry():
    with (
        patch(
            "app.workers.tasks.execute_normalization",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch.object(normalize_run, "retry", side_effect=Retry("delayed")) as retry,
        pytest.raises(Retry) as raised,
    ):
        normalize_run.run(str(uuid7()), str(uuid7()))

    assert str(raised.value) == "delayed"
    assert retry.call_args.kwargs["countdown"] == (get_settings().NORMALIZATION_RETRY_BASE_SECONDS)


@pytest.mark.parametrize("failure", [ValueError("internal value"), TypeError("internal type")])
def test_worker_internal_value_and_type_errors_use_delayed_retry(failure):
    with (
        patch(
            "app.workers.tasks.execute_normalization",
            new=AsyncMock(side_effect=failure),
        ),
        patch.object(normalize_run, "retry", side_effect=Retry("delayed")) as retry,
        pytest.raises(Retry),
    ):
        normalize_run.run(str(uuid7()), str(uuid7()))

    retry.assert_called_once()


@pytest.mark.parametrize(
    ("run_id", "delivery_id"),
    [("invalid", str(uuid7())), (str(uuid7()), None)],
)
def test_worker_rejects_invalid_uuid_without_executing(run_id, delivery_id):
    with (
        patch("app.workers.tasks.execute_normalization", new=AsyncMock()) as execute,
        pytest.raises(Reject),
    ):
        normalize_run.run(run_id, delivery_id)

    execute.assert_not_called()


async def _seed_eod_dataset(session) -> None:
    session.add(
        DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="TW Equity EOD",
            asset_class="equity",
            market="TW",
            frequency="daily",
            is_active=True,
            config={
                "schema_id": "market_eod",
                "accepted_schema_versions": [1],
                "current_schema_version": 1,
                "schema_enforcement": "enforce",
                "allowed_sources": ["finlab"],
                "defaults": {"market": "TW", "asset_class": "equity", "currency": "TWD"},
            },
        )
    )
    await session.commit()


def _request(idempotency_key: str, close: float = 100.0) -> dict:
    return {
        "dataset_key": "tw_equity_eod",
        "schema_id": "market_eod",
        "schema_version": 1,
        "source": "finlab",
        "request_key": idempotency_key,
        "idempotency_key": idempotency_key,
        "payload": {
            "batch": {
                "data_date": "2026-07-15",
                "delivery_mode": "full_snapshot",
                "declared_record_count": 1,
            },
            "data": [
                {
                    "symbol": "2330",
                    "source_symbol": "2330 TT Equity",
                    "trade_date": "2026-07-15",
                    "name": "台積電",
                    "currency": "TWD",
                    "open": close - 1,
                    "high": close + 1,
                    "low": close - 2,
                    "close": close,
                    "volume": 100,
                }
            ],
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("request_count", [10, 100, 500])
async def test_burst_requests_are_durably_accepted(
    request_count: int,
    test_engine,
    test_session,
    source_headers: dict,
):
    await _seed_eod_dataset(test_session)
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def request_scoped_db():
        async with session_factory() as session:
            yield session

    settings = get_settings()
    original_limit = settings.RATE_LIMIT_REQUESTS
    settings.RATE_LIMIT_REQUESTS = 1_000
    source_client = (await test_session.execute(select(SourceClient))).scalar_one()
    source_client.rate_limit_requests = request_count
    await test_session.commit()
    reset_source_rate_limit_state()
    app.dependency_overrides[get_db] = request_scoped_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as burst_client:
            responses = await asyncio.gather(
                *(
                    burst_client.post(
                        "/api/v1/source/ingest",
                        headers=source_headers,
                        json=_request(f"burst-{request_count}-{index}"),
                    )
                    for index in range(request_count)
                )
            )
    finally:
        app.dependency_overrides.clear()
        settings.RATE_LIMIT_REQUESTS = original_limit
        reset_source_rate_limit_state()

    assert {response.status_code for response in responses} == {202}
    async with session_factory() as verification_session:
        assert (
            await verification_session.scalar(select(func.count()).select_from(RawMarketPayload))
            == request_count
        )
        assert (
            await verification_session.scalar(select(func.count()).select_from(NormalizationJob))
            == request_count
        )
        assert (
            await verification_session.scalar(select(func.count()).select_from(NormalizationOutbox))
            == request_count
        )


@pytest.mark.asyncio
async def test_202_atomically_creates_raw_run_job_and_outbox(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_eod_dataset(test_session)

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_request("durable-accept-1"),
    )

    assert response.status_code == 202
    run_id = response.json()["run_id"]
    run = await test_session.get(IngestionRun, run_id)
    raw = (
        await test_session.execute(
            select(RawMarketPayload).where(RawMarketPayload.run_id == run_id)
        )
    ).scalar_one()
    job = (
        await test_session.execute(
            select(NormalizationJob).where(NormalizationJob.run_id == run_id)
        )
    ).scalar_one()
    outbox = (
        await test_session.execute(
            select(NormalizationOutbox).where(NormalizationOutbox.run_id == run_id)
        )
    ).scalar_one()

    assert run is not None and run.status == "queued"
    assert run.raw_payload_id == raw.raw_payload_id
    assert len(raw.payload_sha256 or "") == 64
    assert job.status == "queued"
    assert outbox.status == "pending"
    assert outbox.delivery_id == job.delivery_id


@pytest.mark.asyncio
async def test_scoped_idempotency_payload_mismatch_returns_409(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_eod_dataset(test_session)
    first = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_request("conflicting-key", close=100.0),
    )
    second = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_request("conflicting-key", close=101.0),
    )

    assert first.status_code == 202
    assert second.status_code == 409
    run_count = await test_session.scalar(select(func.count()).select_from(IngestionRun))
    assert run_count == 1


@pytest.mark.asyncio
async def test_source_client_is_bound_to_source_dataset_and_own_runs(
    client: AsyncClient,
    admin_headers: dict,
    source_headers: dict,
    test_session,
):
    await _seed_eod_dataset(test_session)
    issued = await client.post(
        "/api/v1/admin/source-clients",
        headers=admin_headers,
        json={
            "name": "FinLab provider",
            "source_name": "finlab",
            "allowed_datasets": ["tw_equity_eod"],
            "rate_limit_requests": 20,
            "rate_limit_window": 60,
        },
    )
    assert issued.status_code == 200
    provider_headers = {"X-API-Key": issued.json()["api_key"]}

    accepted = await client.post(
        "/api/v1/source/ingest",
        headers=provider_headers,
        json=_request("source-owned-run-1"),
    )
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]

    own_status = await client.get(f"/api/v1/source/runs/{run_id}", headers=provider_headers)
    legacy_status = await client.get(f"/api/v1/source/runs/{run_id}", headers=source_headers)
    assert own_status.status_code == 200
    assert legacy_status.status_code == 404

    mismatched = _request("source-mismatch-1")
    mismatched["source"] = "twelve_data"
    forbidden = await client.post(
        "/api/v1/source/ingest",
        headers=provider_headers,
        json=mismatched,
    )
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_reconciliation_replays_published_delivery_after_broker_loss(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_eod_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_request("broker-wipe-1"),
    )
    run_id = response.json()["run_id"]
    outbox = (
        await test_session.execute(
            select(NormalizationOutbox).where(NormalizationOutbox.run_id == run_id)
        )
    ).scalar_one()
    outbox.status = "published"
    outbox.published_at = utc_now()
    await test_session.commit()

    await reconcile_nonterminal_jobs(test_session)

    pending = (
        (
            await test_session.execute(
                select(NormalizationOutbox).where(
                    NormalizationOutbox.run_id == run_id,
                    NormalizationOutbox.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_reconciliation_resets_expired_processing_lease(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_eod_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_request("expired-lease-1"),
    )
    run_id = response.json()["run_id"]
    job = (
        await test_session.execute(
            select(NormalizationJob).where(NormalizationJob.run_id == run_id)
        )
    ).scalar_one()
    old_delivery_id = job.delivery_id
    job.status = "processing"
    job.lease_expires_at = utc_now() - timedelta(seconds=1)
    outboxes = (
        await test_session.execute(
            select(NormalizationOutbox).where(NormalizationOutbox.run_id == run_id)
        )
    ).scalars()
    for outbox in outboxes:
        outbox.status = "published"
    await test_session.commit()

    await reconcile_nonterminal_jobs(test_session)
    await test_session.refresh(job)

    assert job.status == "queued"
    assert job.lease_expires_at is None
    assert job.delivery_id != old_delivery_id


@pytest.mark.asyncio
async def test_reconciliation_preserves_delivery_id_when_active_outbox_exists(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_eod_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_request("expired-active-delivery"),
    )
    run_id = response.json()["run_id"]
    job = (
        await test_session.execute(
            select(NormalizationJob).where(NormalizationJob.run_id == run_id)
        )
    ).scalar_one()
    delivery_id = job.delivery_id
    job.status = "processing"
    job.lease_expires_at = utc_now() - timedelta(seconds=1)
    await test_session.commit()

    assert await reconcile_nonterminal_jobs(test_session) == 0
    await test_session.refresh(job)
    outbox = (
        await test_session.execute(
            select(NormalizationOutbox).where(NormalizationOutbox.run_id == run_id)
        )
    ).scalar_one()

    assert job.status == "queued"
    assert job.delivery_id == delivery_id
    assert outbox.delivery_id == delivery_id


@pytest.mark.asyncio
async def test_reconciliation_never_rewrites_mismatched_active_generation(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_eod_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_request("mismatched-active-delivery"),
    )
    run_id = response.json()["run_id"]
    job = (
        await test_session.execute(
            select(NormalizationJob).where(NormalizationJob.run_id == run_id)
        )
    ).scalar_one()
    outbox = (
        await test_session.execute(
            select(NormalizationOutbox).where(NormalizationOutbox.run_id == run_id)
        )
    ).scalar_one()
    active_delivery_id = outbox.delivery_id
    current_delivery_id = uuid7()
    job.delivery_id = current_delivery_id
    job.status = "processing"
    job.lease_expires_at = utc_now() - timedelta(seconds=1)
    await test_session.commit()

    assert await reconcile_nonterminal_jobs(test_session) == 0
    await test_session.refresh(job)
    await test_session.refresh(outbox)
    assert job.status == "queued"
    assert job.delivery_id == current_delivery_id
    assert outbox.delivery_id == active_delivery_id

    assert await reconcile_stale_jobs(test_session) == 0
    assert (
        await test_session.scalar(
            select(func.count(NormalizationOutbox.outbox_id)).where(
                NormalizationOutbox.job_id == job.job_id,
                NormalizationOutbox.status.in_(("pending", "publishing")),
            )
        )
        == 1
    )
    outbox.status = "published"
    outbox.published_at = utc_now()
    job.available_at = utc_now() - timedelta(seconds=1)
    await test_session.commit()

    assert await reconcile_stale_jobs(test_session) == 1
    await test_session.refresh(job)
    repaired_delivery_id = job.delivery_id
    assert repaired_delivery_id not in {current_delivery_id, active_delivery_id}
    active = (
        (
            await test_session.execute(
                select(NormalizationOutbox).where(
                    NormalizationOutbox.job_id == job.job_id,
                    NormalizationOutbox.status.in_(("pending", "publishing")),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(active) == 1
    assert active[0].delivery_id == repaired_delivery_id
    assert await reconcile_stale_jobs(test_session) == 0


@pytest.mark.asyncio
async def test_stale_reconciliation_repairs_processing_job_without_lease(test_session):
    await _seed_eod_dataset(test_session)
    now = utc_now()
    run = IngestionRun(
        dataset_key="tw_equity_eod",
        source="finlab",
        status="processing",
        created_at=now - timedelta(hours=1),
    )
    test_session.add(run)
    await test_session.flush()
    delivery_id = uuid7()
    job = NormalizationJob(
        run_id=run.run_id,
        dataset_key="tw_equity_eod",
        status="processing",
        delivery_id=delivery_id,
        lease_expires_at=None,
        available_at=now - timedelta(hours=1),
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    test_session.add(job)
    await test_session.flush()
    test_session.add(
        NormalizationOutbox(
            job_id=job.job_id,
            run_id=run.run_id,
            delivery_id=delivery_id,
            status="published",
            available_at=now - timedelta(hours=1),
            published_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    await test_session.commit()

    assert await reconcile_stale_jobs(test_session) == 1
    await test_session.refresh(job)
    assert job.status == "queued"
    assert job.delivery_id != delivery_id
    assert await reconcile_stale_jobs(test_session) == 0


@pytest.mark.asyncio
async def test_lock_contention_creates_one_new_delivery_for_concurrent_duplicates(
    test_engine,
    test_session,
):
    await _seed_eod_dataset(test_session)
    now = utc_now()
    run = IngestionRun(
        dataset_key="tw_equity_eod",
        source="finlab",
        status="queued",
        created_at=now,
    )
    test_session.add(run)
    await test_session.flush()
    original_delivery_id = uuid7()
    job = NormalizationJob(
        run_id=run.run_id,
        dataset_key="tw_equity_eod",
        status="queued",
        delivery_id=original_delivery_id,
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    test_session.add(job)
    await test_session.flush()
    test_session.add(
        NormalizationOutbox(
            job_id=job.job_id,
            run_id=run.run_id,
            delivery_id=original_delivery_id,
            status="published",
            available_at=now,
            published_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    await test_session.commit()
    job_id = job.job_id
    run_id = run.run_id

    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_factory() as first, session_factory() as second:
        results = await asyncio.gather(
            _defer_for_lock_contention(
                first,
                job_id=job_id,
                run_id=run_id,
                delivery_id=original_delivery_id,
            ),
            _defer_for_lock_contention(
                second,
                job_id=job_id,
                run_id=run_id,
                delivery_id=original_delivery_id,
            ),
        )

    assert sum(results) == 1
    test_session.expire_all()
    refreshed = await test_session.get(NormalizationJob, job_id)
    assert refreshed is not None
    assert refreshed.delivery_id != original_delivery_id
    rows = (
        (
            await test_session.execute(
                select(NormalizationOutbox).where(NormalizationOutbox.job_id == job_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert (
        sum(row.status == "pending" and row.delivery_id == refreshed.delivery_id for row in rows)
        == 1
    )

    assert (
        await _defer_for_lock_contention(
            test_session,
            job_id=job_id,
            run_id=run_id,
            delivery_id=refreshed.delivery_id,
        )
        is False
    )
    assert (
        await test_session.scalar(
            select(func.count(NormalizationOutbox.outbox_id)).where(
                NormalizationOutbox.job_id == job_id
            )
        )
        == 2
    )


@pytest.mark.asyncio
async def test_lock_contention_refreshes_preloaded_job_before_deferring(
    test_engine,
    test_session,
):
    await _seed_eod_dataset(test_session)
    now = utc_now()
    run = IngestionRun(
        dataset_key="tw_equity_eod",
        source="finlab",
        status="queued",
        created_at=now,
    )
    test_session.add(run)
    await test_session.flush()
    delivery_id = uuid7()
    job = NormalizationJob(
        run_id=run.run_id,
        dataset_key="tw_equity_eod",
        status="queued",
        delivery_id=delivery_id,
        available_at=now,
        created_at=now,
        updated_at=now,
    )
    test_session.add(job)
    await test_session.flush()
    test_session.add(
        NormalizationOutbox(
            job_id=job.job_id,
            run_id=run.run_id,
            delivery_id=delivery_id,
            status="published",
            available_at=now,
            published_at=now,
            created_at=now,
            updated_at=now,
        )
    )
    await test_session.commit()
    job_id = job.job_id
    run_id = run.run_id

    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_factory() as stale_session, session_factory() as updater_session:
        preloaded = await stale_session.get(NormalizationJob, job_id)
        assert preloaded is not None
        assert preloaded.status == "queued"

        updated = await updater_session.get(NormalizationJob, job_id)
        assert updated is not None
        updated.status = "completed"
        updated.completed_at = utc_now()
        updated.updated_at = utc_now()
        await updater_session.commit()

        assert (
            await _defer_for_lock_contention(
                stale_session,
                job_id=job_id,
                run_id=run_id,
                delivery_id=delivery_id,
            )
            is False
        )
        assert preloaded.status == "completed"

    test_session.expire_all()
    refreshed = await test_session.get(NormalizationJob, job_id)
    assert refreshed is not None
    assert refreshed.status == "completed"
    assert refreshed.delivery_id == delivery_id
    assert (
        await test_session.scalar(
            select(func.count(NormalizationOutbox.outbox_id)).where(
                NormalizationOutbox.job_id == job_id
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_stale_published_delivery_is_recreated(test_session):
    await _seed_eod_dataset(test_session)
    now = utc_now()
    run = IngestionRun(
        dataset_key="tw_equity_eod",
        source="finlab",
        status="queued",
        created_at=now,
    )
    test_session.add(run)
    await test_session.flush()
    job = NormalizationJob(
        run_id=run.run_id,
        dataset_key="tw_equity_eod",
        status="queued",
        delivery_id=uuid7(),
        available_at=now - timedelta(hours=1),
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    test_session.add(job)
    await test_session.flush()
    old_delivery_id = job.delivery_id
    test_session.add(
        NormalizationOutbox(
            job_id=job.job_id,
            run_id=run.run_id,
            delivery_id=old_delivery_id,
            status="published",
            available_at=now - timedelta(hours=1),
            published_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=1),
            updated_at=now - timedelta(hours=1),
        )
    )
    await test_session.commit()

    repaired = await reconcile_stale_jobs(test_session)
    await test_session.refresh(job)

    assert repaired == 1
    assert job.delivery_id != old_delivery_id
    assert job.status == "queued"
    pending = await test_session.scalar(
        select(func.count(NormalizationOutbox.outbox_id)).where(
            NormalizationOutbox.job_id == job.job_id,
            NormalizationOutbox.status == "pending",
        )
    )
    assert pending == 1

    pending_row = (
        await test_session.execute(
            select(NormalizationOutbox).where(
                NormalizationOutbox.job_id == job.job_id,
                NormalizationOutbox.status == "pending",
            )
        )
    ).scalar_one()
    pending_row.status = "published"
    pending_row.published_at = utc_now()
    await test_session.commit()
    current_delivery_id = job.delivery_id

    assert await reconcile_stale_jobs(test_session) == 0
    await test_session.refresh(job)
    assert job.delivery_id == current_delivery_id


@pytest.mark.asyncio
async def test_raw_cleanup_skips_database_when_disabled():
    with patch("scripts.cleanup_raw.create_async_engine") as create_engine:
        deleted = await cleanup_expired_raw(retention_enabled=False)

    assert deleted == 0
    create_engine.assert_not_called()


@pytest.mark.asyncio
async def test_raw_cleanup_preserves_nonterminal_and_unpublished_work(
    test_session,
    test_engine,
):
    await _seed_eod_dataset(test_session)
    now = utc_now()
    retention_days = get_settings().RAW_RETENTION_DAYS
    old_terminal_at = now - timedelta(days=retention_days + 1)
    recently_terminal_at = now - timedelta(days=1)
    expired_at = now - timedelta(days=1)

    async def add_work(
        key: str,
        status: str,
        outbox_status: str,
        *,
        terminal_at: datetime | None = None,
    ):
        run_id = uuid7()
        raw = RawMarketPayload(
            raw_payload_id=uuid7(),
            dataset_key="tw_equity_eod",
            source="finlab",
            request_key=key,
            idempotency_key=key,
            payload_sha256="a" * 64,
            payload={"data": []},
            fetched_at=old_terminal_at,
            expire_at=expired_at,
            run_id=run_id,
            created_at=old_terminal_at,
        )
        run = IngestionRun(
            run_id=run_id,
            dataset_key="tw_equity_eod",
            raw_payload_id=raw.raw_payload_id,
            status=status,
            completed_at=terminal_at,
            created_at=old_terminal_at,
        )
        job = NormalizationJob(
            job_id=uuid7(),
            run_id=run_id,
            dataset_key="tw_equity_eod",
            status=status,
            delivery_id=uuid7(),
            available_at=now,
            created_at=old_terminal_at,
            updated_at=now,
            completed_at=terminal_at,
        )
        outbox = NormalizationOutbox(
            outbox_id=uuid7(),
            job_id=job.job_id,
            run_id=run_id,
            delivery_id=job.delivery_id,
            status=outbox_status,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        test_session.add_all((raw, run, job, outbox))
        return raw.raw_payload_id

    deletable_ids = [
        await add_work(
            f"{status}-published",
            status,
            "published",
            terminal_at=old_terminal_at,
        )
        for status in ("completed", "completed_with_errors", "failed")
    ]
    queued_id = await add_work("queued-pending", "queued", "pending")
    unpublished_id = await add_work(
        "terminal-pending",
        "completed",
        "pending",
        terminal_at=old_terminal_at,
    )
    recent_terminal_id = await add_work(
        "recent-terminal-published",
        "completed",
        "published",
        terminal_at=recently_terminal_at,
    )
    legacy_run_id = uuid7()
    legacy_raw = RawMarketPayload(
        raw_payload_id=uuid7(),
        dataset_key="tw_equity_eod",
        source="finlab",
        request_key="legacy-terminal",
        idempotency_key="legacy-terminal",
        payload_sha256="b" * 64,
        payload={"data": []},
        fetched_at=old_terminal_at,
        expire_at=expired_at,
        run_id=legacy_run_id,
        created_at=old_terminal_at,
    )
    legacy_run = IngestionRun(
        run_id=legacy_run_id,
        dataset_key="tw_equity_eod",
        raw_payload_id=legacy_raw.raw_payload_id,
        status="completed",
        completed_at=None,
        created_at=old_terminal_at,
    )
    legacy_raw_id = legacy_raw.raw_payload_id
    test_session.add_all((legacy_raw, legacy_run))
    await test_session.commit()

    deleted = await cleanup_expired_raw(
        database_url=test_engine.url.render_as_string(hide_password=False),
        retention_enabled=True,
    )
    test_session.expire_all()

    assert deleted == 4
    for raw_payload_id in deletable_ids:
        assert await test_session.get(RawMarketPayload, raw_payload_id) is None
    assert await test_session.get(RawMarketPayload, queued_id) is not None
    assert await test_session.get(RawMarketPayload, unpublished_id) is not None
    assert await test_session.get(RawMarketPayload, recent_terminal_id) is not None
    assert await test_session.get(RawMarketPayload, legacy_raw_id) is None
