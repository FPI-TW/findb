"""DB-authoritative normalization queue and reconciliation tests."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from celery.exceptions import Retry
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import reset_source_rate_limit_state
from app.config import get_settings
from app.dependencies import get_db
from app.main import app
from app.models.canonical import InstrumentStats, MarketDataEOD
from app.models.raw import RawMarketPayload
from app.models.registry import (
    DatasetRegistry,
    DQIssue,
    IngestionRun,
    NormalizationJob,
    NormalizationOutbox,
)
from app.services.normalization_queue import (
    reconcile_nonterminal_jobs,
    reconcile_stale_jobs,
)
from app.services.normalize.base import BaseNormalizer
from app.task_queue import normalization_queue
from app.utils import utc_now, uuid7
from app.workers.tasks import normalize_run
from scripts.cleanup_raw import cleanup_expired_raw


class _PrecedenceNormalizer(BaseNormalizer):
    dataset_key = "crypto_eod"
    asset_class = "crypto"
    market = "CRYPTO"

    def map_fields(self, raw_data: dict):
        return self.map_fields_from_config(raw_data)


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


async def _seed_crypto_dataset(session) -> None:
    session.add(
        DatasetRegistry(
            dataset_key="crypto_eod",
            name="Crypto EOD",
            asset_class="crypto",
            market="CRYPTO",
            frequency="daily",
            is_active=True,
            config={
                "data_path": "data",
                "field_mapping": {
                    "symbol": "symbol",
                    "trade_date": "trade_date",
                    "close": "close",
                },
            },
        )
    )
    await session.commit()


def _request(idempotency_key: str, close: float = 100.0) -> dict:
    return {
        "dataset_key": "crypto_eod",
        "source": "bloomberg",
        "request_key": idempotency_key,
        "idempotency_key": idempotency_key,
        "payload": {
            "data": [
                {
                    "symbol": "BTCUSD",
                    "trade_date": "2026-07-15",
                    "close": close,
                }
            ]
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
    await _seed_crypto_dataset(test_session)
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
    reset_source_rate_limit_state()
    app.dependency_overrides[get_db] = request_scoped_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as burst_client:
            responses = await asyncio.gather(
                *(
                    burst_client.post(
                        "/api/v1/source/ingest/crypto",
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
    await _seed_crypto_dataset(test_session)

    response = await client.post(
        "/api/v1/source/ingest/crypto",
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
    await _seed_crypto_dataset(test_session)
    first = await client.post(
        "/api/v1/source/ingest/crypto",
        headers=source_headers,
        json=_request("conflicting-key", close=100.0),
    )
    second = await client.post(
        "/api/v1/source/ingest/crypto",
        headers=source_headers,
        json=_request("conflicting-key", close=101.0),
    )

    assert first.status_code == 202
    assert second.status_code == 409
    run_count = await test_session.scalar(select(func.count()).select_from(IngestionRun))
    assert run_count == 1


@pytest.mark.asyncio
async def test_direct_endpoint_honors_idempotency_key_header(
    client: AsyncClient,
    source_headers: dict,
):
    headers = {**source_headers, "Idempotency-Key": "provider-direct-key-1"}
    payload = {
        "metadata": {"source": "bloomberg", "query_time": "2026-07-15T00:00:00Z"},
        "data": [
            {
                "symbol": "BTCUSD",
                "date": "2026-07-15",
                "open": 100,
                "high": 110,
                "low": 90,
                "close": 105,
                "volume": 1,
            }
        ],
    }
    first = await client.post("/api/v1/source/ingest/crypto/direct", headers=headers, json=payload)
    payload["data"][0]["close"] = 106
    second = await client.post("/api/v1/source/ingest/crypto/direct", headers=headers, json=payload)

    assert first.status_code == 202
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_source_client_is_bound_to_source_dataset_and_own_runs(
    client: AsyncClient,
    admin_headers: dict,
    source_headers: dict,
    test_session,
):
    await _seed_crypto_dataset(test_session)
    issued = await client.post(
        "/api/v1/admin/source-clients",
        headers=admin_headers,
        json={
            "name": "Bloomberg provider",
            "source_name": "bloomberg",
            "allowed_datasets": ["crypto_eod"],
            "rate_limit_requests": 20,
            "rate_limit_window": 60,
        },
    )
    assert issued.status_code == 200
    provider_headers = {"X-API-Key": issued.json()["api_key"]}

    accepted = await client.post(
        "/api/v1/source/ingest/crypto",
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
    mismatched["source"] = "finlab"
    forbidden = await client.post(
        "/api/v1/source/ingest/crypto",
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
    await _seed_crypto_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest/crypto",
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
    await _seed_crypto_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest/crypto",
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
    await _seed_crypto_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest/crypto",
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
async def test_stale_published_delivery_is_recreated(test_session):
    await _seed_crypto_dataset(test_session)
    now = utc_now()
    run = IngestionRun(
        dataset_key="crypto_eod",
        source="bloomberg",
        status="queued",
        created_at=now,
    )
    test_session.add(run)
    await test_session.flush()
    job = NormalizationJob(
        run_id=run.run_id,
        dataset_key="crypto_eod",
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
async def test_source_precedence_is_independent_of_worker_completion_order(test_session):
    await _seed_crypto_dataset(test_session)
    runs = [
        IngestionRun(
            dataset_key="crypto_eod",
            source=source,
            status="queued",
            created_at=utc_now(),
        )
        for source in ("preferred", "fallback", "preferred")
    ]
    test_session.add_all(runs)
    await test_session.commit()
    run_ids = [run.run_id for run in runs]

    base_config = {
        "source_precedence": ["preferred", "fallback"],
        "source_field": "source",
        "field_mapping": {
            "symbol": "symbol",
            "trade_date": "date",
            "close": "close",
        },
    }

    async def normalize(run_id, source: str, close: int, fetched_at: datetime):
        normalizer = _PrecedenceNormalizer(
            test_session,
            {**base_config, "_ingest_fetched_at": fetched_at},
        )
        return await normalizer.process(
            {
                "data": [
                    {
                        "symbol": "BTCUSD",
                        "date": "2026-07-15",
                        "close": close,
                        "source": source,
                    }
                ]
            },
            run_id,
        )

    await normalize(run_ids[0], "preferred", 100, datetime(2026, 7, 15, tzinfo=timezone.utc))
    fallback_result = await normalize(
        run_ids[1], "fallback", 999, datetime(2026, 7, 16, tzinfo=timezone.utc)
    )
    row = (await test_session.execute(select(MarketDataEOD))).scalar_one()
    assert int(row.close or 0) == 100
    assert row.source == "preferred"
    assert fallback_result.success_records == 0
    assert fallback_result.failed_records == 0
    assert fallback_result.precedence_rejected_records == 1
    warning = (
        await test_session.execute(
            select(DQIssue).where(
                DQIssue.run_id == run_ids[1],
                DQIssue.issue_type == "SOURCE_PRECEDENCE_REJECTED",
            )
        )
    ).scalar_one()
    assert warning.severity == "warning"
    assert warning.raw_data == {"rejected_records": 1}
    stats = await test_session.get(InstrumentStats, row.instrument_id)
    assert stats is not None
    assert int(stats.latest_price or 0) == 100

    await normalize(run_ids[2], "preferred", 101, datetime(2026, 7, 17, tzinfo=timezone.utc))
    test_session.expire_all()
    row = (await test_session.execute(select(MarketDataEOD))).scalar_one()
    assert int(row.close or 0) == 101


@pytest.mark.asyncio
async def test_raw_cleanup_preserves_nonterminal_and_unpublished_work(
    test_session,
    test_engine,
):
    await _seed_crypto_dataset(test_session)
    now = utc_now()

    async def add_work(key: str, status: str, outbox_status: str):
        run_id = uuid7()
        raw = RawMarketPayload(
            raw_payload_id=uuid7(),
            dataset_key="crypto_eod",
            source="bloomberg",
            request_key=key,
            idempotency_key=key,
            payload_sha256="a" * 64,
            payload={"data": []},
            fetched_at=now - timedelta(days=30),
            expire_at=now - timedelta(days=16),
            run_id=run_id,
            created_at=now - timedelta(days=30),
        )
        run = IngestionRun(
            run_id=run_id,
            dataset_key="crypto_eod",
            raw_payload_id=raw.raw_payload_id,
            status=status,
            completed_at=(now - timedelta(days=15) if status == "completed" else None),
            created_at=now - timedelta(days=30),
        )
        job = NormalizationJob(
            job_id=uuid7(),
            run_id=run_id,
            dataset_key="crypto_eod",
            status=status,
            delivery_id=uuid7(),
            available_at=now,
            created_at=now - timedelta(days=30),
            updated_at=now,
            completed_at=(now - timedelta(days=15) if status == "completed" else None),
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

    deletable_id = await add_work("terminal-published", "completed", "published")
    queued_id = await add_work("queued-pending", "queued", "pending")
    unpublished_id = await add_work("terminal-pending", "completed", "pending")
    legacy_run_id = uuid7()
    legacy_raw = RawMarketPayload(
        raw_payload_id=uuid7(),
        dataset_key="crypto_eod",
        source="bloomberg",
        request_key="legacy-terminal",
        idempotency_key="legacy-terminal",
        payload_sha256="b" * 64,
        payload={"data": []},
        fetched_at=now - timedelta(days=30),
        expire_at=now - timedelta(days=16),
        run_id=legacy_run_id,
        created_at=now - timedelta(days=30),
    )
    legacy_run = IngestionRun(
        run_id=legacy_run_id,
        dataset_key="crypto_eod",
        raw_payload_id=legacy_raw.raw_payload_id,
        status="completed",
        completed_at=None,
        created_at=now - timedelta(days=30),
    )
    legacy_raw_id = legacy_raw.raw_payload_id
    test_session.add_all((legacy_raw, legacy_run))
    await test_session.commit()

    deleted = await cleanup_expired_raw(
        database_url=test_engine.url.render_as_string(hide_password=False),
        retention_enabled=True,
    )
    test_session.expire_all()

    assert deleted == 2
    assert await test_session.get(RawMarketPayload, deletable_id) is None
    assert await test_session.get(RawMarketPayload, queued_id) is not None
    assert await test_session.get(RawMarketPayload, unpublished_id) is not None
    assert await test_session.get(RawMarketPayload, legacy_raw_id) is None
