"""The FinLab daily start moves to 19:30 and its intraday feeds to 19:35 (7733f9692fe6)."""

import json
import subprocess

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "ebb187773e30"
MOVED = "7733f9692fe6"
DAILY = ("tw_equity_eod", "tw_etf_eod")
INTRADAY = ("tw_equity_bar_1m", "tw_etf_bar_1m", "tw_equity_tick", "tw_etf_tick")


async def _clocks(engine) -> dict[str, str]:
    async with engine.connect() as db:
        rows = await db.execute(
            text(
                "SELECT dataset_key, config #>> '{delivery_expectation,schedule,local_time}' "
                "FROM dataset_registry WHERE dataset_key IN :keys"
            ).bindparams(sa.bindparam("keys", expanding=True)),
            {"keys": [*DAILY, *INTRADAY]},
        )
        clocks = dict(rows.all())
        control = await db.scalar(
            text(
                "SELECT scheduled_local_time::text FROM scheduler_control "
                "WHERE scheduler_key = 'full_market_finlab_v1'"
            )
        )
    clocks["control"] = control
    return clocks


def _expected(daily: str, intraday: str) -> dict[str, str]:
    return {
        **{key: daily for key in DAILY},
        **{key: intraday for key in INTRADAY},
        "control": daily,
    }


@pytest.mark.asyncio
async def test_finlab_starts_move_and_return_on_downgrade():
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "finlab_daily_1930"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        assert await _clocks(engine) == _expected("18:00:00", "19:15:00")

        await run_alembic(url, MOVED)
        assert await _clocks(engine) == _expected("19:30:00", "19:35:00")
        # The monitor clock and every other governed value stay as they were.
        async with engine.connect() as db:
            monitor = await db.scalar(
                text(
                    "SELECT config #> '{delivery_expectation,missing_delivery}' "
                    "FROM dataset_registry WHERE dataset_key = 'tw_equity_eod'"
                )
            )
        assert monitor["deadline_local_time"] == "17:00:00"

        await run_alembic(url, BEFORE, command="downgrade")
        assert await _clocks(engine) == _expected("18:00:00", "19:15:00")


@pytest.mark.asyncio
async def test_an_unknown_finlab_start_aborts_before_any_update():
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "finlab_daily_1930_unknown"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE dataset_registry SET config = jsonb_set(config, "
                    "'{delivery_expectation,schedule,local_time}', CAST(:clock AS jsonb)) "
                    "WHERE dataset_key = 'tw_etf_eod'"
                ),
                {"clock": json.dumps("18:45:00")},
            )
        with pytest.raises(subprocess.CalledProcessError) as failure:
            await run_alembic(url, MOVED)
        assert "tw_etf_eod" in failure.value.stderr
        clocks = await _clocks(engine)
        assert clocks["tw_equity_eod"] == "18:00:00"
        assert clocks["control"] == "18:00:00"
        assert clocks["tw_equity_bar_1m"] == "19:15:00"


@pytest.mark.asyncio
async def test_an_unknown_intraday_start_aborts_the_upgrade():
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "finlab_intraday_1935_unknown"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE dataset_registry SET config = jsonb_set(config, "
                    "'{delivery_expectation,schedule,local_time}', CAST(:clock AS jsonb)) "
                    "WHERE dataset_key = 'tw_etf_tick'"
                ),
                {"clock": json.dumps("20:00:00")},
            )
        with pytest.raises(subprocess.CalledProcessError) as failure:
            await run_alembic(url, MOVED)
        assert "tw_etf_tick" in failure.value.stderr
        assert (await _clocks(engine))["tw_equity_eod"] == "18:00:00"
