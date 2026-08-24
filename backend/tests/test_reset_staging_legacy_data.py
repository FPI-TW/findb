from __future__ import annotations

import argparse

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base
from app.models.canonical import CalendarMarket, Instrument, MarketDataEOD, TradingCalendar
from app.models.raw import RawMarketPayload
from app.models.registry import (
    APIKey,
    DatasetRegistry,
    IngestionAttempt,
    IngestionRun,
    NormalizationJob,
    NormalizationOutbox,
    NormalizationWorkerHeartbeat,
    SchedulerControl,
    SchedulerDataset,
    SourceClient,
)
from app.utils import utc_now, uuid7
from scripts.reset_staging_legacy_data import (
    PROTECTED_TABLES,
    STAGING_CONFIRMATION,
    TARGET_TABLES,
    ResetSafetyError,
    get_target_fingerprint,
    reset_staging_legacy_data,
    validate_cli_safety,
)


def test_reset_table_inventory_covers_every_application_model() -> None:
    model_tables = {
        f"{table.schema or 'public'}.{table.name}" for table in Base.metadata.tables.values()
    }
    declared_tables = set(TARGET_TABLES) | (set(PROTECTED_TABLES) - {"public.alembic_version"})

    assert declared_tables == model_tables
    assert set(TARGET_TABLES).isdisjoint(PROTECTED_TABLES)


def test_apply_requires_exact_staging_and_writer_confirmations() -> None:
    with pytest.raises(ResetSafetyError, match="confirm-staging"):
        validate_cli_safety(
            argparse.Namespace(
                apply=True,
                confirm_staging="staging",
                confirm_target_fingerprint="0" * 64,
                confirm_db_writers_stopped=True,
            )
        )

    with pytest.raises(ResetSafetyError, match="confirm-db-writers-stopped"):
        validate_cli_safety(
            argparse.Namespace(
                apply=True,
                confirm_staging=STAGING_CONFIRMATION,
                confirm_target_fingerprint="0" * 64,
                confirm_db_writers_stopped=False,
            )
        )

    validate_cli_safety(
        argparse.Namespace(
            apply=False,
            confirm_staging=None,
            confirm_target_fingerprint=None,
            confirm_db_writers_stopped=False,
        )
    )

    with pytest.raises(ResetSafetyError, match="confirm-target-fingerprint"):
        validate_cli_safety(
            argparse.Namespace(
                apply=True,
                confirm_staging=STAGING_CONFIRMATION,
                confirm_target_fingerprint=None,
                confirm_db_writers_stopped=True,
            )
        )


async def _seed_protected_rows(session: AsyncSession) -> tuple[DatasetRegistry, SourceClient]:
    dataset = DatasetRegistry(
        dataset_key="test.eod",
        name="Test EOD",
        asset_class="equity",
        market="US",
    )
    source_client = SourceClient(
        name="staging-fetcher",
        source_name="twelve_data",
        key_hash="a" * 64,
    )
    session.add_all(
        [
            dataset,
            source_client,
            APIKey(key_hash="b" * 64, owner="staging-test"),
            SchedulerControl(
                scheduler_key="staging-reset-test",
                provider="test-provider",
                slot_id="taiwan_market_window",
            ),
        ]
    )
    await session.flush()
    session.add(
        SchedulerDataset(
            scheduler_key="staging-reset-test",
            dataset_key="test.eod",
        )
    )
    await session.flush()
    return dataset, source_client


async def _seed_run_graph(
    session: AsyncSession,
    dataset: DatasetRegistry,
    source_client: SourceClient,
    *,
    status: str,
    suffix: str,
) -> IngestionRun:
    now = utc_now()
    run_id = uuid7()
    raw = RawMarketPayload(
        source_client_id=source_client.client_id,
        dataset_key=dataset.dataset_key,
        source="twelve_data",
        request_key=f"request-{suffix}",
        idempotency_key=f"idempotency-{suffix}",
        payload={"data": []},
        fetched_at=now,
        expire_at=now,
        run_id=run_id,
    )
    session.add(raw)
    await session.flush()
    run = IngestionRun(
        run_id=run_id,
        dataset_key=dataset.dataset_key,
        source="twelve_data",
        source_client_id=source_client.client_id,
        raw_payload_id=raw.raw_payload_id,
        request_key=f"request-{suffix}",
        status=status,
    )
    session.add(run)
    await session.flush()
    job = NormalizationJob(
        run_id=run.run_id,
        dataset_key=dataset.dataset_key,
        status=status,
    )
    session.add(job)
    await session.flush()
    session.add_all(
        [
            NormalizationOutbox(
                job_id=job.job_id,
                run_id=run.run_id,
                delivery_id=job.delivery_id,
                status="published",
            ),
            IngestionAttempt(
                source_client_id=source_client.client_id,
                run_id=run.run_id,
                dataset_key=dataset.dataset_key,
                source="twelve_data",
                request_key=f"request-{suffix}",
                idempotency_key=f"idempotency-{suffix}",
                status="accepted",
            ),
        ]
    )
    await session.flush()
    return run


@pytest.mark.asyncio
async def test_dry_run_accepts_all_current_terminal_statuses(test_engine) -> None:
    async with test_engine.begin() as connection:
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            dataset, source_client = await _seed_protected_rows(session)
            for status in ("completed", "completed_with_errors", "failed"):
                await _seed_run_graph(
                    session,
                    dataset,
                    source_client,
                    status=status,
                    suffix=status,
                )

            report = await reset_staging_legacy_data(connection, apply=False)

            assert report.mode == "dry-run"
            assert report.before == report.after
            assert report.before["public.ingestion_run"] == 3
            assert report.before["public.normalization_job"] == 3


@pytest.mark.asyncio
async def test_target_fingerprint_is_stable_and_exposed_by_dry_run(test_engine) -> None:
    async with test_engine.begin() as connection:
        first = await reset_staging_legacy_data(connection, apply=False)
        second = await reset_staging_legacy_data(connection, apply=False)

        assert first.target_fingerprint == second.target_fingerprint
        assert first.target_fingerprint == await get_target_fingerprint(connection)
        assert len(first.target_fingerprint) == 64
        assert set(first.target_fingerprint) <= set("0123456789abcdef")


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [None, "0" * 64])
async def test_apply_refuses_missing_or_wrong_target_fingerprint(
    test_engine, confirmation: str | None
) -> None:
    async with test_engine.begin() as connection:
        with pytest.raises(ResetSafetyError, match="fresh dry run"):
            await reset_staging_legacy_data(
                connection,
                apply=True,
                db_writers_stopped=True,
                target_fingerprint=confirmation,
            )


@pytest.mark.asyncio
async def test_reset_refuses_nonterminal_work_even_with_writer_confirmation(test_engine) -> None:
    async with test_engine.begin() as connection:
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            dataset, source_client = await _seed_protected_rows(session)
            await _seed_run_graph(
                session,
                dataset,
                source_client,
                status="processing",
                suffix="active",
            )

            with pytest.raises(ResetSafetyError, match=r"runs=1, jobs=1"):
                await reset_staging_legacy_data(
                    connection,
                    apply=True,
                    db_writers_stopped=True,
                    target_fingerprint=await get_target_fingerprint(connection),
                )


@pytest.mark.asyncio
async def test_apply_truncates_mutable_tables_and_preserves_protected_tables(test_engine) -> None:
    async with test_engine.begin() as connection:
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            dataset, source_client = await _seed_protected_rows(session)
            run = await _seed_run_graph(
                session,
                dataset,
                source_client,
                status="completed",
                suffix="reset",
            )
            now = utc_now()
            instrument = Instrument(asset_class="equity", market="US", symbol="RESET")
            session.add_all(
                [
                    instrument,
                    CalendarMarket(
                        market="US",
                        display_name="美國",
                        timezone="America/New_York",
                        weekend_days=[5, 6],
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    MarketDataEOD(
                        instrument_id=instrument.instrument_id,
                        trade_date=now.date(),
                        close=100,
                        source="twelve_data",
                        asof_ts=now,
                        run_id=run.run_id,
                    ),
                    TradingCalendar(
                        market="US",
                        trade_date=now.date(),
                        is_open=True,
                    ),
                    NormalizationWorkerHeartbeat(
                        worker_id="staging-worker",
                        current_run_id=run.run_id,
                    ),
                ]
            )
            await session.flush()

            dry_run = await reset_staging_legacy_data(connection, apply=False)
            report = await reset_staging_legacy_data(
                connection,
                apply=True,
                db_writers_stopped=True,
                target_fingerprint=dry_run.target_fingerprint,
            )

            assert report.mode == "apply"
            assert report.before["public.market_data_eod"] == 1
            assert report.before["raw.market_payload"] == 1
            assert all(report.after[table_name] == 0 for table_name in TARGET_TABLES)
            assert report.after["public.dataset_registry"] == 1
            assert report.after["public.scheduler_control"] == 1
            assert report.after["public.scheduler_dataset"] == 1
            assert report.after["public.source_client"] == 1
            assert report.after["public.api_key"] == 1
            assert report.after["public.calendar_market"] == 1
            assert await connection.scalar(text("SELECT count(*) FROM dataset_registry")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM scheduler_control")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM scheduler_dataset")) == 1
