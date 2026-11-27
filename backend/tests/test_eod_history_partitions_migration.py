"""Yearly market_data_eod partitions for 2000-2025 (e0649188f071) and their rollback guards."""

import subprocess
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

TEMPLATE = "1587769858c0"
PREVIOUS = "e5979e281ba9"
REVISION = "e0649188f071"
YEARS = {f"market_data_eod_y{year}" for year in range(2000, 2026)}


async def _partitions(engine) -> set[str]:
    async with engine.connect() as connection:
        return set(
            (
                await connection.execute(
                    text(
                        "SELECT child.relname FROM pg_inherits "
                        "JOIN pg_class child ON child.oid = pg_inherits.inhrelid "
                        "WHERE pg_inherits.inhparent = 'market_data_eod'::regclass"
                    )
                )
            ).scalars()
        )


async def _eod_row(engine, trade_date: str) -> None:
    instrument = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO instruments (instrument_id, asset_class, market, symbol, status, "
                "created_at, updated_at) VALUES (:id, 'equity', 'US', :symbol, 'active', now(), now())"
            ),
            {"id": instrument, "symbol": f"T{instrument.hex[:8]}"},
        )
        await connection.execute(
            text(
                "INSERT INTO market_data_eod (instrument_id, trade_date, close, asof_ts, "
                "created_at, updated_at) VALUES (:id, :day, 1, now(), now(), now())"
            ),
            {"id": instrument, "day": date.fromisoformat(trade_date)},
        )


async def _routed(engine, trade_date: str) -> str:
    async with engine.connect() as connection:
        return await connection.scalar(
            text("SELECT tableoid::regclass::text FROM market_data_eod WHERE trade_date = :day"),
            {"day": date.fromisoformat(trade_date)},
        )


@pytest.mark.asyncio
async def test_upgrade_attaches_2000_through_2025_and_downgrade_drops_empty_ones():
    async with get_active_migration_database_factory().clone(TEMPLATE, "eod_years") as (
        url,
        engine,
    ):
        await run_alembic(url, PREVIOUS)
        before = await _partitions(engine)
        assert not before & YEARS

        await run_alembic(url, REVISION)
        assert await _partitions(engine) == before | YEARS
        await _eod_row(engine, "2010-06-01")
        assert await _routed(engine, "2010-06-01") == "market_data_eod_y2010"

        # A backfilled year blocks the rollback instead of being dropped with it.
        with pytest.raises(subprocess.CalledProcessError) as refused:
            await run_alembic(url, PREVIOUS, command="downgrade")
        assert "hold backfilled rows" in refused.value.stderr
        assert await _partitions(engine) == before | YEARS

        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM market_data_eod_y2010"))
        await run_alembic(url, PREVIOUS, command="downgrade")
        assert await _partitions(engine) == before


@pytest.mark.asyncio
async def test_upgrade_refuses_while_the_default_partition_holds_rows_in_range():
    async with get_active_migration_database_factory().clone(TEMPLATE, "eod_default") as (
        url,
        engine,
    ):
        await run_alembic(url, PREVIOUS)
        await _eod_row(engine, "2015-03-02")
        assert await _routed(engine, "2015-03-02") == "market_data_eod_default"
        with pytest.raises(subprocess.CalledProcessError) as refused:
            await run_alembic(url, REVISION)
        assert "market_data_eod_default holds 1 rows" in refused.value.stderr
        assert not await _partitions(engine) & YEARS
