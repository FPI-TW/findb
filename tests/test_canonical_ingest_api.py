"""Integration tests for canonical ingest attempts and versioned contracts."""

from datetime import timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.canonical import FuturesContinuousEOD, Instrument, MarketDataEOD
from app.models.raw import RawMarketPayload
from app.models.registry import (
    DatasetRegistry,
    IngestionAttempt,
    IngestionRun,
    NormalizationJob,
    NormalizationOutbox,
)
from app.services.ingestion_attempts import reconcile_stale_ingestion_attempts
from app.services.normalization_queue import execute_normalization
from app.services.source_clients import create_source_client
from app.utils import utc_now, uuid7


def _dataset_config() -> dict:
    return {
        "schema_id": "market_eod",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "audit",
        "defaults": {
            "market": "TW",
            "asset_class": "equity",
            "currency": "TWD",
        },
        "delivery_expectation": {
            "delivery_mode": "full_snapshot",
            "freshness_hours": 36,
            "minimum_record_count": 1,
            "maximum_count_drop_ratio": 0.1,
        },
    }


def _canonical_request(*, idempotency_key: str = "finlab_tw_equity_eod_20260721") -> dict:
    return {
        "dataset_key": "tw_equity_eod",
        "schema_id": "market_eod",
        "schema_version": 1,
        "source": "finlab",
        "request_key": "finlab_tw_equity_eod_20260721_01",
        "idempotency_key": idempotency_key,
        "fetched_at": "2026-07-21T08:00:00Z",
        "payload": {
            "batch": {
                "data_date": "2026-07-21",
                "delivery_mode": "full_snapshot",
                "declared_record_count": 1,
            },
            "data": [
                {
                    "symbol": "2330",
                    "source_symbol": "2330 TT Equity",
                    "trade_date": "2026-07-21",
                    "name": "台積電",
                    "currency": "TWD",
                    "open": "1000",
                    "high": "1020",
                    "low": "995",
                    "close": "1015",
                    "volume": 32_100_000,
                }
            ],
        },
    }


def _futures_request() -> dict:
    return {
        "dataset_key": "wtx_eod",
        "schema_id": "futures_continuous_eod",
        "schema_version": 1,
        "source": "bloomberg",
        "request_key": "bloomberg_wtx_eod_20260721_01",
        "idempotency_key": "bloomberg_wtx_eod_20260721",
        "fetched_at": "2026-07-21T08:00:00Z",
        "payload": {
            "batch": {
                "data_date": "2026-07-21",
                "delivery_mode": "full_snapshot",
                "declared_record_count": 1,
            },
            "data": [
                {
                    "symbol": "TX",
                    "source_symbol": "TXA Index",
                    "trade_date": "2026-07-21",
                    "name": "臺股期貨近月",
                    "open": "23000",
                    "high": "23200",
                    "low": "22900",
                    "close": "23150",
                    "volume": 80_000,
                    "open_interest": 120_000,
                    "active_contract_code": "TXF202607",
                    "roll_rule": "front_month",
                }
            ],
        },
    }


async def _seed_dataset(test_session, *, config: dict | None = None) -> None:
    test_session.add(
        DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="TW Equity EOD",
            asset_class="equity",
            market="TW",
            frequency="daily",
            is_active=True,
            config=config if config is not None else _dataset_config(),
        )
    )
    await test_session.commit()


async def _seed_futures_dataset(test_session) -> None:
    test_session.add(
        DatasetRegistry(
            dataset_key="wtx_eod",
            name="WTX Continuous EOD",
            asset_class="future",
            market="WTX",
            frequency="daily",
            is_active=True,
            config={
                "schema_id": "futures_continuous_eod",
                "accepted_schema_versions": [1],
                "current_schema_version": 1,
                "schema_enforcement": "audit",
                "defaults": {
                    "market": "WTX",
                    "asset_class": "future",
                    "currency": "TWD",
                },
            },
        )
    )
    await test_session.commit()


async def _execute_run(test_session, test_engine, run_id: UUID) -> None:
    job = (
        await test_session.execute(
            select(NormalizationJob).where(NormalizationJob.run_id == run_id)
        )
    ).scalar_one()
    await execute_normalization(
        run_id,
        job.delivery_id,
        database_url=test_engine.url.render_as_string(hide_password=False),
    )
    test_session.expire_all()


@pytest.mark.asyncio
async def test_canonical_ingest_persists_attempt_raw_run_and_job(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 202
    body = response.json()
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    run = await test_session.get(IngestionRun, UUID(body["run_id"]))
    raw = await test_session.get(RawMarketPayload, run.raw_payload_id)
    assert attempt is not None
    assert attempt.status == "accepted"
    assert attempt.run_id == run.run_id
    assert run.schema_id == "market_eod"
    assert run.schema_version == 1
    assert raw.schema_id == "market_eod"
    assert raw.schema_version == 1
    assert raw.payload["data"][0]["trade_date"] == "2026-07-21"
    assert (
        await test_session.scalar(
            select(func.count())
            .select_from(NormalizationJob)
            .where(NormalizationJob.run_id == run.run_id)
        )
        == 1
    )
    assert (
        await test_session.scalar(
            select(func.count())
            .select_from(NormalizationOutbox)
            .where(NormalizationOutbox.run_id == run.run_id)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_dataset_discovery_exposes_contract_declaration(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)

    response = await client.get("/api/v1/source/datasets", headers=source_headers)

    assert response.status_code == 200
    dataset = response.json()["data"][0]
    assert dataset["schema_id"] == "market_eod"
    assert dataset["accepted_schema_versions"] == [1]
    assert dataset["current_schema_version"] == 1
    assert dataset["schema_enforcement"] == "audit"


@pytest.mark.asyncio
async def test_canonical_market_eod_normalizes_without_provider_specific_fields(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
):
    await _seed_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    run_id = UUID(response.json()["run_id"])

    await _execute_run(test_session, test_engine, run_id)

    instrument = (
        await test_session.execute(select(Instrument).where(Instrument.symbol == "2330"))
    ).scalar_one()
    eod = (
        await test_session.execute(
            select(MarketDataEOD).where(MarketDataEOD.instrument_id == instrument.instrument_id)
        )
    ).scalar_one()
    run = await test_session.get(IngestionRun, run_id)
    assert instrument.market == "TW"
    assert instrument.asset_class == "equity"
    assert instrument.currency == "TWD"
    assert str(eod.close) == "1015.00000000"
    assert eod.source == "finlab"
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_canonical_futures_contract_routes_by_schema_not_provider_payload(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
):
    await _seed_futures_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_futures_request(),
    )
    assert response.status_code == 202
    run_id = UUID(response.json()["run_id"])

    await _execute_run(test_session, test_engine, run_id)

    instrument = (
        await test_session.execute(select(Instrument).where(Instrument.symbol == "TX"))
    ).scalar_one()
    eod = (
        await test_session.execute(
            select(FuturesContinuousEOD).where(
                FuturesContinuousEOD.instrument_id == instrument.instrument_id
            )
        )
    ).scalar_one()
    run = await test_session.get(IngestionRun, run_id)
    assert instrument.market == "WTX"
    assert instrument.currency == "TWD"
    assert str(eod.close) == "23150.00000000"
    assert eod.source == "bloomberg"
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_canonical_raw_rerun_preserves_schema_lineage_and_routing(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
):
    await _seed_dataset(test_session)
    accepted = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    original_run_id = UUID(accepted.json()["run_id"])
    rerun = await client.post(
        f"/api/v1/source/runs/{original_run_id}/rerun",
        headers=source_headers,
    )
    rerun_id = UUID(rerun.json()["run_id"])

    await _execute_run(test_session, test_engine, rerun_id)

    rerun_row = await test_session.get(IngestionRun, rerun_id)
    assert rerun.status_code == 202
    assert rerun_row.schema_id == "market_eod"
    assert rerun_row.schema_version == 1
    assert rerun_row.status == "completed"


@pytest.mark.asyncio
async def test_worker_routes_using_persisted_schema_version(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
):
    await _seed_dataset(test_session)
    accepted = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    run_id = UUID(accepted.json()["run_id"])
    run = await test_session.get(IngestionRun, run_id)
    raw = await test_session.get(RawMarketPayload, run.raw_payload_id)
    raw.schema_version = 2
    await test_session.commit()

    await _execute_run(test_session, test_engine, run_id)

    await test_session.refresh(run)
    assert run.status == "failed"
    assert run.failure_code == "NORMALIZER_NOT_CONFIGURED"
    assert await test_session.scalar(select(func.count()).select_from(MarketDataEOD)) == 0


@pytest.mark.asyncio
async def test_schema_invalid_request_is_durably_rejected(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    value = _canonical_request()
    value["payload"]["data"][0]["price"] = {"last": 1015, "secret": "do-not-leak"}

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INGRESS_SCHEMA_INVALID"
    assert "do-not-leak" not in body["error"]["message"]
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.failure_code == "INGRESS_SCHEMA_INVALID"
    assert attempt.http_status == 422
    assert attempt.run_id is None
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0


@pytest.mark.asyncio
async def test_invalid_json_is_durably_rejected(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    response = await client.post(
        "/api/v1/source/ingest",
        headers={**source_headers, "Content-Type": "application/json"},
        content=b'{"dataset_key":',
    )

    assert response.status_code == 422
    body = response.json()
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert body["error"]["code"] == "INGRESS_SCHEMA_INVALID"
    assert attempt.status == "rejected"
    assert attempt.dataset_key is None
    assert attempt.request_sha256 is not None


@pytest.mark.asyncio
async def test_unknown_dataset_rejection_has_attempt_id(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    value = _canonical_request()
    value["dataset_key"] = "unknown_dataset"

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "DATASET_NOT_FOUND"
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert attempt.dataset_key == "unknown_dataset"
    assert attempt.status == "rejected"


@pytest.mark.asyncio
async def test_dataset_without_contract_is_rejected_and_recorded(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session, config={"source_format": "legacy"})

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "DATASET_CONTRACT_NOT_CONFIGURED"
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert attempt.status == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config_update",
    [
        {"defaults": None},
        {"defaults": {"market": "US", "asset_class": "equity", "currency": "TWD"}},
        {"defaults": {"market": "TW", "asset_class": "etf", "currency": "TWD"}},
    ],
)
async def test_invalid_or_conflicting_contract_scope_is_rejected_before_queueing(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    config_update: dict,
):
    config = _dataset_config()
    config.update(config_update)
    await _seed_dataset(test_session, config=config)

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DATASET_CONTRACT_NOT_CONFIGURED"
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0
    assert await test_session.scalar(select(func.count()).select_from(NormalizationJob)) == 0


@pytest.mark.asyncio
async def test_duplicate_contract_creates_new_attempt_but_reuses_run(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)
    first = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    second = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["run_id"] == first.json()["run_id"]
    assert second.json()["attempt_id"] != first.json()["attempt_id"]
    second_attempt = await test_session.get(
        IngestionAttempt,
        UUID(second.json()["attempt_id"]),
    )
    assert second_attempt.status == "duplicate"
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 1
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 1


@pytest.mark.asyncio
async def test_idempotency_mismatch_is_rejected_without_new_run(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)
    first = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    changed = _canonical_request()
    changed["payload"]["data"][0]["close"] = "1016"
    second = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=changed,
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    attempt = await test_session.get(IngestionAttempt, UUID(second.json()["attempt_id"]))
    assert attempt.status == "rejected"
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 1


@pytest.mark.asyncio
async def test_idempotency_key_cannot_be_reused_by_different_source(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)
    first = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    changed_source = _canonical_request()
    changed_source["source"] = "bloomberg"
    second = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=changed_source,
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"


@pytest.mark.asyncio
async def test_unbounded_schema_version_does_not_break_attempt_persistence(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    value = _canonical_request()
    value["schema_version"] = 10**100

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 422
    attempt = await test_session.get(IngestionAttempt, UUID(response.json()["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.schema_version is None


@pytest.mark.asyncio
async def test_attempt_status_endpoint_returns_rejection(
    client: AsyncClient,
    source_headers: dict,
):
    value = _canonical_request()
    value["schema_version"] = 2
    rejected = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    response = await client.get(
        f"/api/v1/source/attempts/{rejected.json()['attempt_id']}",
        headers=source_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["failure_code"] == "INGRESS_SCHEMA_UNSUPPORTED"


@pytest.mark.asyncio
async def test_attempt_status_is_scoped_to_authenticated_source_client(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)
    _, finlab_key = await create_source_client(
        test_session,
        name="FinLab fetcher",
        source_name="finlab",
        allowed_datasets=["tw_equity_eod"],
        rate_limit_requests=100,
        rate_limit_window=60,
    )
    _, other_key = await create_source_client(
        test_session,
        name="Other fetcher",
        source_name="other",
        allowed_datasets=["tw_equity_eod"],
        rate_limit_requests=100,
        rate_limit_window=60,
    )
    header_name = next(iter(source_headers))
    rejected = await client.post(
        "/api/v1/source/ingest",
        headers={header_name: finlab_key},
        json={**_canonical_request(), "source": "bloomberg"},
    )
    attempt_id = rejected.json()["attempt_id"]

    owner_response = await client.get(
        f"/api/v1/source/attempts/{attempt_id}",
        headers={header_name: finlab_key},
    )
    other_response = await client.get(
        f"/api/v1/source/attempts/{attempt_id}",
        headers={header_name: other_key},
    )

    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "SOURCE_IDENTITY_MISMATCH"
    assert owner_response.status_code == 200
    assert other_response.status_code == 404


@pytest.mark.asyncio
async def test_unexpected_processing_error_marks_attempt_rejected(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    monkeypatch,
):
    await _seed_dataset(test_session)

    async def fail_ingest(*args, **kwargs):
        raise RuntimeError("sensitive internal failure")

    monkeypatch.setattr(
        "app.services.canonical_ingestion.IngestionService.ingest_contract",
        fail_ingest,
    )
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "sensitive" not in response.json()["error"]["message"]
    attempt = await test_session.get(IngestionAttempt, UUID(response.json()["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.failure_code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_database_failure_before_attempt_returns_retryable_503(
    client: AsyncClient,
    source_headers: dict,
    monkeypatch,
):
    async def fail_begin(*args, **kwargs):
        raise OperationalError("INSERT", {}, RuntimeError("database unavailable"))

    monkeypatch.setattr(
        "app.services.canonical_ingestion.IngestionAttemptService.begin",
        fail_begin,
    )
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.json() == {
        "success": False,
        "attempt_id": None,
        "error": {
            "code": "DATABASE_UNAVAILABLE",
            "message": "Ingestion is temporarily unavailable",
        },
    }


@pytest.mark.asyncio
async def test_stale_received_attempts_are_reconciled_in_bounded_batches(test_session):
    stale = IngestionAttempt(
        attempt_id=uuid7(),
        status="received",
        created_at=utc_now() - timedelta(minutes=10),
        updated_at=utc_now() - timedelta(minutes=10),
    )
    recent = IngestionAttempt(
        attempt_id=uuid7(),
        status="received",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    stale_two = IngestionAttempt(
        attempt_id=uuid7(),
        status="received",
        created_at=utc_now() - timedelta(minutes=9),
        updated_at=utc_now() - timedelta(minutes=9),
    )
    stale_three = IngestionAttempt(
        attempt_id=uuid7(),
        status="received",
        created_at=utc_now() - timedelta(minutes=8),
        updated_at=utc_now() - timedelta(minutes=8),
    )
    test_session.add_all([stale, stale_two, stale_three, recent])
    await test_session.commit()

    assert (
        await reconcile_stale_ingestion_attempts(
            test_session,
            stale_seconds=300,
            batch_size=2,
        )
        == 2
    )
    assert (
        await reconcile_stale_ingestion_attempts(
            test_session,
            stale_seconds=300,
            batch_size=2,
        )
        == 1
    )
    await test_session.refresh(stale)
    await test_session.refresh(recent)
    assert stale.status == "aborted"
    assert stale.failure_code == "ATTEMPT_INTERRUPTED"
    assert stale.completed_at is not None
    assert recent.status == "received"


@pytest.mark.asyncio
async def test_live_attempt_claim_is_skipped_until_owner_releases(
    test_session,
    test_engine,
):
    stale = IngestionAttempt(
        attempt_id=uuid7(),
        status="received",
        created_at=utc_now() - timedelta(minutes=10),
        updated_at=utc_now() - timedelta(minutes=10),
    )
    test_session.add(stale)
    await test_session.commit()
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as active_session:
        claimed = await active_session.scalar(
            select(IngestionAttempt)
            .where(IngestionAttempt.attempt_id == stale.attempt_id)
            .with_for_update()
        )
        assert claimed is not None
        async with session_factory() as reconcile_session:
            assert (
                await reconcile_stale_ingestion_attempts(
                    reconcile_session,
                    stale_seconds=300,
                    batch_size=10,
                )
                == 0
            )
        await active_session.rollback()

    async with session_factory() as reconcile_session:
        assert (
            await reconcile_stale_ingestion_attempts(
                reconcile_session,
                stale_seconds=300,
                batch_size=10,
            )
            == 1
        )


@pytest.mark.asyncio
async def test_reconciliation_never_reverses_terminal_attempt(test_session):
    accepted = IngestionAttempt(
        attempt_id=uuid7(),
        status="accepted",
        http_status=202,
        created_at=utc_now() - timedelta(minutes=10),
        updated_at=utc_now() - timedelta(minutes=10),
        completed_at=utc_now() - timedelta(minutes=9),
    )
    test_session.add(accepted)
    await test_session.commit()

    assert (
        await reconcile_stale_ingestion_attempts(
            test_session,
            stale_seconds=300,
            batch_size=10,
        )
        == 0
    )
    await test_session.refresh(accepted)
    assert accepted.status == "accepted"
    assert accepted.failure_code is None
