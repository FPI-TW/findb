"""FactSet Hong Kong 20:00 start: frozen projection, atomic abort and reversible downgrade."""

import subprocess
from copy import deepcopy
from datetime import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import DatasetRegistry, SchedulerControl
from scripts.preflight_catalog_schedule import migration_module
from scripts.seed_data import seed_datasets
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "a9c1e3f5b7d0"
HEAD = "b3d5f7a9c1e2"
CONTROL = "full_market_factset_v1"


async def _previous_clock(engine, *, drift: bool = False) -> None:
    """Seed the live catalog, then put Hong Kong back on the deployed 18:30 start."""
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await seed_datasets(db)
        # This revision predates the HKEX feed (d8e0f2a4c6b9), which it cannot judge.
        for statement in (
            "DELETE FROM scheduler_dataset WHERE scheduler_key = 'full_market_hkex_v1'",
            "DELETE FROM scheduler_control WHERE scheduler_key = 'full_market_hkex_v1'",
            "DELETE FROM dataset_registry WHERE dataset_key = 'hk_equity_official_eod'",
        ):
            await db.execute(text(statement))
        dataset = await db.get(DatasetRegistry, "hk_equity_eod")
        config = deepcopy(dataset.config)
        config["operator_note"] = "retain"
        config["delivery_expectation"]["schedule"]["local_time"] = "18:30:00"
        dataset.config = config
        control = await db.get(SchedulerControl, CONTROL)
        control.scheduled_local_time = time(18, 30)
        control.desired_state = "running"
        if drift:
            us = await db.get(DatasetRegistry, "us_equity_eod")
            changed = deepcopy(us.config)
            changed["delivery_expectation"]["schedule"]["local_time"] = "12:34:00"
            us.config = changed
        await db.commit()


async def _clocks(engine) -> tuple[str, str, dict]:
    async with engine.connect() as db:
        row = (
            await db.execute(
                text(
                    "SELECT c.scheduled_local_time::text, "
                    "d.config#>>'{delivery_expectation,schedule,local_time}', d.config "
                    "FROM scheduler_control c, dataset_registry d "
                    "WHERE c.scheduler_key=:key AND d.dataset_key='hk_equity_eod'"
                ),
                {"key": CONTROL},
            )
        ).one()
    return row[0], row[1], row[2]


@pytest.mark.asyncio
async def test_hong_kong_start_moves_to_2000_and_downgrade_restores_1830():
    module = migration_module()
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "factset_hk_clock"
    ) as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        await _previous_clock(engine)
        async with engine.connect() as db:
            report, _, _ = await db.run_sync(module.inspect)
        assert {row["key"]: row["status"] for row in report}["hk_equity_eod"] == (
            "known_old_default"
        )
        assert {row["key"]: row["status"] for row in report}[CONTROL] == "known_old_default"
        assert not any(row["status"] == "unknown_difference" for row in report)

        await run_alembic(url, HEAD)
        control_clock, dataset_clock, config = await _clocks(engine)
        assert (control_clock, dataset_clock) == ("20:00:00", "20:00:00")
        assert config["operator_note"] == "retain"
        async with engine.connect() as db:
            report, _, _ = await db.run_sync(module.inspect)
            state = await db.scalar(
                text("SELECT desired_state FROM scheduler_control WHERE scheduler_key=:key"),
                {"key": CONTROL},
            )
        assert all(row["status"] == "already_compliant" for row in report)
        assert state == "running"

        await run_alembic(url, BEFORE, command="downgrade")
        control_clock, dataset_clock, config = await _clocks(engine)
        assert (control_clock, dataset_clock) == ("18:30:00", "18:30:00")
        assert config["operator_note"] == "retain"


@pytest.mark.asyncio
async def test_unknown_schedule_difference_aborts_without_moving_hong_kong():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "factset_hk_drift"
    ) as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        await _previous_clock(engine, drift=True)
        with pytest.raises(subprocess.CalledProcessError) as failed:
            await run_alembic(url, HEAD)
        assert "unknown differences" in failed.value.stderr
        control_clock, dataset_clock, _ = await _clocks(engine)
        assert (control_clock, dataset_clock) == ("18:30:00", "18:30:00")
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == BEFORE


@pytest.mark.asyncio
async def test_pre_projection_upgrade_passes_preflight_and_reaches_head():
    """The accepted upgrade base still carries the TW bootstrap policy and Taipei clocks."""
    module = migration_module()
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "factset_hk_base") as (
        url,
        engine,
    ):
        await run_alembic(url, "d8e3f0b5a2c4")
        async with engine.connect() as db:
            report, _, _ = await db.run_sync(module.inspect)
        assert not [row for row in report if row["status"] == "unknown_difference"]
        await run_alembic(url, HEAD)
        async with engine.connect() as db:
            report, _, _ = await db.run_sync(module.inspect)
        assert all(row["status"] == "already_compliant" for row in report)


def test_frozen_inputs_match_the_live_catalog():
    from app.services.provider_metadata import acquisition_feeds

    module = migration_module()
    for feed in acquisition_feeds():
        if feed.dataset_key in module.FEEDS:
            provider, zone, clock, *_ = module.FEEDS[feed.dataset_key]
            assert feed.schedule is not None
            assert (feed.provider, feed.schedule.timezone) == (provider, zone)
            assert time.fromisoformat(feed.schedule.local_time).isoformat() == clock
