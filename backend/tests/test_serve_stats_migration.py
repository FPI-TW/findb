"""Migration coverage for domain-specific Serve instrument stats."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory

BACKEND_ROOT = Path(__file__).resolve().parents[1]


async def upgrade_head(database_url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_domain_stats_backfill_uses_canonical_data_and_preserves_legacy_columns() -> None:
    async with get_active_migration_database_factory().clone("a8b9c0d1e2f3", "serve_stats") as (
        database_url,
        engine,
    ):
        equity_id, etf_id, empty_id = uuid7(), uuid7(), uuid7()
        async with engine.begin() as connection:
            for instrument_id, asset_class, symbol in (
                (equity_id, "equity", "2330"),
                (etf_id, "etf", "0050"),
                (empty_id, "equity", "2317"),
            ):
                await connection.execute(
                    text("""
                        INSERT INTO instruments (
                            instrument_id, asset_class, market, symbol, status,
                            created_at, updated_at
                        ) VALUES (:id, :asset_class, 'TW', :symbol, 'active', now(), now())
                    """),
                    {"id": instrument_id, "asset_class": asset_class, "symbol": symbol},
                )
                await connection.execute(
                    text("""
                        INSERT INTO instrument_stats (
                            instrument_id, first_trade_date, latest_trade_date,
                            latest_price, updated_at
                        ) VALUES (:id, DATE '1999-01-01', DATE '1999-01-02', 9, now())
                    """),
                    {"id": instrument_id},
                )
            await connection.execute(
                text("""
                    INSERT INTO market_data_eod (
                        instrument_id, trade_date, close, asof_ts, created_at, updated_at
                    ) VALUES
                        (:id, DATE '2026-09-17', 100, now(), now(), now()),
                        (:id, DATE '2026-09-18', 101, now(), now(), now())
                """),
                {"id": equity_id},
            )
            await connection.execute(
                text("""
                    INSERT INTO market_data_minute (
                        instrument_id, trade_date, bar_start_time, bar_end_time,
                        signal_time, market_timezone, open, high, low, close,
                        volume, turnover, trade_count, price_adjustment, source,
                        source_priority, source_fetched_at, asof_ts, created_at, updated_at
                    ) VALUES
                        (:id, DATE '2026-09-18', TIMESTAMPTZ '2026-09-18 01:00:00+00',
                         TIMESTAMPTZ '2026-09-18 01:01:00+00',
                         TIMESTAMPTZ '2026-09-18 01:01:00+00', 'Asia/Taipei',
                         50, 50, 50, 50, 10, 500, NULL, 'none', 'shioaji', 1000,
                         now(), now(), now(), now()),
                        (:id, DATE '2026-09-18', TIMESTAMPTZ '2026-09-18 01:01:00+00',
                         TIMESTAMPTZ '2026-09-18 01:02:00+00',
                         TIMESTAMPTZ '2026-09-18 01:02:00+00', 'Asia/Taipei',
                         51, 51, 51, 51, 10, 510, NULL, 'none', 'shioaji', 1000,
                         now(), now(), now(), now())
                """),
                {"id": etf_id},
            )

        await upgrade_head(database_url)

        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text("""
                        SELECT instrument_id, first_trade_date, latest_trade_date, latest_price,
                               eod_first_date, eod_latest_date, eod_latest_close,
                               minute_first_bar_at, minute_latest_bar_at, minute_latest_close
                        FROM instrument_stats
                        ORDER BY instrument_id
                    """)
                )
            ).mappings()
            by_id = {str(row["instrument_id"]): row for row in rows}

        equity = by_id[str(equity_id)]
        assert str(equity["first_trade_date"]) == "1999-01-01"
        assert str(equity["latest_trade_date"]) == "1999-01-02"
        assert equity["latest_price"] == 9
        assert str(equity["eod_first_date"]) == "2026-09-17"
        assert str(equity["eod_latest_date"]) == "2026-09-18"
        assert equity["eod_latest_close"] == 101

        etf = by_id[str(etf_id)]
        assert etf["eod_first_date"] is None
        assert str(etf["minute_first_bar_at"]) == "2026-09-18 01:00:00+00:00"
        assert str(etf["minute_latest_bar_at"]) == "2026-09-18 01:01:00+00:00"
        assert etf["minute_latest_close"] == 51

        empty = by_id[str(empty_id)]
        assert empty["eod_first_date"] is None
        assert empty["minute_first_bar_at"] is None
