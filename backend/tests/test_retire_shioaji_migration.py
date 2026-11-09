"""Retirement is atomic, refuses unfinished delivery and preserves historical evidence."""

import subprocess
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import SourceClient
from app.services.source_clients import hash_source_key
from app.utils import uuid7
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic
from tests.test_shioaji_backend_retirement import _retained_history

BEFORE = "c7d2e9a4f1b3"
HEAD = "d8e3f0b5a2c4"
EVIDENCE_TABLES = (
    "historical_backfill_request",
    "historical_backfill_item",
    "ingestion_run",
    "provider_work",
    "provider_work_attempt",
    "provider_work_owner",
    "provider_work_prepared",
    "provider_work_receipt",
    "scheduler_dataset",
    "market_data_minute",
    "daily_delivery_plan",
    "daily_delivery_part",
)


async def _snapshot(engine):
    async with engine.connect() as db:
        return {
            table: (await db.execute(text(f"SELECT row_to_json(t) FROM {table} t"))).scalars().all()
            for table in EVIDENCE_TABLES
        }


@pytest.mark.asyncio
async def test_shioaji_retirement_preserves_evidence_and_never_resurrects_credentials():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "retire_shioaji") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            records = await _retained_history(db)
            from app.models.registry import DatasetRegistry, SchedulerControl

            # Pre-profile controls were retired without rewriting their last observation.
            db.add(
                SchedulerControl(
                    scheduler_key="long-retired-shioaji",
                    provider="shioaji",
                    slot_id="taiwan_market_window",
                    timezone="Asia/Taipei",
                    desired_state="stopped",
                    observed_state="running",
                    retired_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                )
            )
            dataset = await db.get(DatasetRegistry, "tw_equity_minute")
            config = deepcopy(
                next(d["config"] for d in DATASETS if d["dataset_key"] == dataset.dataset_key)
            )
            config["allowed_sources"] = ["shioaji", "future_source"]
            config["source_precedence"] = ["shioaji"]
            config["delivery_expectation"]["schedule"].update(
                enabled=True, expected_sources=["shioaji"]
            )
            config["delivery_expectation"]["missing_delivery"].update(
                action="alert", expected_sources=["shioaji", "future_source"]
            )
            dataset.config = config
            for source in ("shioaji", "finlab"):
                db.add(
                    SourceClient(
                        client_id=uuid7(),
                        name=source,
                        source_name=source,
                        key_hash=hash_source_key(source),
                        allowed_datasets=[dataset.dataset_key],
                    )
                )
            from tests.test_serve_api import instrument, minute

            instrument_id = uuid7()
            bar = minute(instrument_id, datetime(2026, 3, 31, 5, 30, tzinfo=timezone.utc), "1301")
            bar.source = "shioaji"
            db.add(instrument(instrument_id, "2330"))
            await db.flush()
            db.add(bar)
            await db.commit()
            work_id, attempt_id = records[2].work_id, records[3].attempt_id

        with pytest.raises(subprocess.CalledProcessError) as failed:
            await run_alembic(url, HEAD)
        assert "unsettled evidence" in failed.value.stderr
        async with engine.begin() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM source_client WHERE revoked_at IS NOT NULL")
                )
                == 0
            )
            # The staging closed-calendar attempts are inert, durable historical evidence.
            await db.execute(
                text("UPDATE provider_work SET state='calendar_closed' WHERE work_id=:id"),
                {"id": work_id},
            )
            assert (
                await db.scalar(
                    text("SELECT state FROM provider_work_attempt WHERE attempt_id=:id"),
                    {"id": attempt_id},
                )
                == "queued"
            )
        before = await _snapshot(engine)
        await run_alembic(url, HEAD)
        assert await _snapshot(engine) == before
        async with engine.connect() as db:
            updated = await db.scalar(
                text("SELECT config FROM dataset_registry WHERE dataset_key='tw_equity_minute'")
            )
            assert updated["allowed_sources"] == ["future_source"]
            assert updated["source_precedence"] == ["shioaji"]
            assert updated["delivery_expectation"]["schedule"]["enabled"] is False
            assert updated["delivery_expectation"]["schedule"]["expected_sources"] == []
            assert updated["delivery_expectation"]["missing_delivery"]["expected_sources"] == [
                "future_source"
            ]
            assert await db.scalar(
                text("SELECT is_active FROM dataset_registry WHERE dataset_key='tw_equity_minute'")
            )
            assert (
                await db.scalar(
                    text("SELECT revoked_at FROM source_client WHERE source_name='shioaji'")
                )
                is not None
            )
            assert (
                await db.scalar(
                    text("SELECT revoked_at FROM source_client WHERE source_name='finlab'")
                )
                is None
            )
            assert (
                await db.scalar(
                    text("SELECT retired_at FROM scheduler_control WHERE provider='shioaji'")
                )
                is not None
            )
        await run_alembic(url, BEFORE, command="downgrade")
        await run_alembic(url, HEAD)
        assert await _snapshot(engine) == before


@pytest.mark.parametrize(
    "blocker", ["controls", "owners", "attempts", "prepared", "historical", "runs"]
)
async def test_retirement_refuses_each_unsettled_boundary(blocker):
    from sqlalchemy import select

    from app.models.registry import SchedulerControl

    async with get_active_migration_database_factory().clone("3553ed95cfbf", "retire_guard") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            request, _, work, attempt, run, owner, _, receipt = await _retained_history(db)
            work.state = "completed"
            attempt.state = "settled"
            if blocker == "controls":
                control = await db.scalar(
                    select(SchedulerControl).where(SchedulerControl.scheduler_key == "old-shioaji")
                )
                control.desired_state = "running"
            elif blocker == "owners":
                owner.attempt_id = attempt.attempt_id
            elif blocker == "attempts":
                attempt.state = "queued"
            elif blocker == "prepared":
                await db.delete(receipt)
            elif blocker == "historical":
                request.status = "running"
            else:
                run.status = "running"
            await db.commit()
        before = await _snapshot(engine)
        with pytest.raises(subprocess.CalledProcessError) as failed:
            await run_alembic(url, HEAD)
        assert f"'{blocker}':" in failed.value.stderr
        assert await _snapshot(engine) == before
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == BEFORE
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM scheduler_control WHERE retired_at IS NOT NULL AND scheduler_key='old-shioaji'"
                    )
                )
                == 0
            )
