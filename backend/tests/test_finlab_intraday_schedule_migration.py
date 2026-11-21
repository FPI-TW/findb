"""FinLab intraday 19:15 start: stored schedules follow seed, unknown ones abort, downgrade restores."""

import subprocess
from copy import deepcopy

import pytest
from sqlalchemy import text

from app.services.provider_metadata import acquisition_feeds
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "e1a3c5e7f9b2"
HEAD = "d7f9b1c3e5a8"
INTRADAY = ("tw_equity_bar_1m", "tw_etf_bar_1m", "tw_equity_tick", "tw_etf_tick")


async def _configs(engine) -> dict[str, dict]:
    async with engine.connect() as db:
        rows = await db.execute(
            text("SELECT dataset_key, config FROM dataset_registry WHERE dataset_key = ANY(:keys)"),
            {"keys": list(INTRADAY)},
        )
        return dict(rows.all())


def _starts(configs: dict[str, dict]) -> set[str]:
    return {config["delivery_expectation"]["schedule"]["local_time"] for config in configs.values()}


@pytest.mark.asyncio
async def test_intraday_start_moves_to_1915_and_downgrade_restores_1830():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "finlab_intraday_1915"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        before = await _configs(engine)
        assert set(before) == set(INTRADAY) and _starts(before) == {"18:30:00"}

        await run_alembic(url, HEAD)
        moved = await _configs(engine)
        # Exactly what seed declares, so provisioning finds nothing to repair.
        assert moved == {
            definition["dataset_key"]: definition["config"]
            for definition in DATASETS
            if definition["dataset_key"] in INTRADAY
        }
        # Only the start changed.
        for key, config in moved.items():
            restored = deepcopy(config)
            restored["delivery_expectation"]["schedule"]["local_time"] = "18:30:00"
            assert restored == before[key]

        await run_alembic(url, BEFORE, command="downgrade")
        assert await _configs(engine) == before


@pytest.mark.asyncio
async def test_unknown_start_aborts_without_moving_any_feed():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "finlab_intraday_drift"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE dataset_registry SET config = jsonb_set(config, "
                    "'{delivery_expectation,schedule,local_time}', '\"20:00:00\"') "
                    "WHERE dataset_key = 'tw_etf_tick'"
                )
            )
        with pytest.raises(subprocess.CalledProcessError) as failed:
            await run_alembic(url, HEAD)
        assert "unknown differences: tw_etf_tick" in failed.value.stderr
        configs = await _configs(engine)
        assert {key: _starts({key: config}) for key, config in configs.items()} == {
            "tw_equity_bar_1m": {"18:30:00"},
            "tw_etf_bar_1m": {"18:30:00"},
            "tw_equity_tick": {"18:30:00"},
            "tw_etf_tick": {"20:00:00"},
        }
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == BEFORE


def test_frozen_start_matches_the_live_catalog():
    feeds = {feed.dataset_key: feed for feed in acquisition_feeds()}
    for key in INTRADAY:
        schedule = feeds[key].schedule
        assert schedule is not None
        assert (schedule.timezone, schedule.local_time) == ("Asia/Taipei", "19:15")
