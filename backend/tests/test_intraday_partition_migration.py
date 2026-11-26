"""The intraday index is additive, constrained, and never dropped while it has rows."""

import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "c5e7a9b1d3f6"
INTRADAY_INDEX = "d6f8b0c2e4a7"
DAY_LAYOUT_INDEX = "e7a9c1d3f5b8"

INSERT = (
    "INSERT INTO intraday_partition(dataset_key,symbol,trade_date,row_count,source,"
    "source_raw_sha256,source_rows_sha256,source_fetched_at,object_layout,object_key,object_etag,"
    "created_at,updated_at) VALUES('tw_equity_bar_1m',:symbol,'2026-11-13',:rows,'finlab',:sha,"
    ":rows_sha,now(),:layout,:key,:etag,now(),now())"
)
VALID = {
    "symbol": "0050",
    "rows": 266,
    "sha": "a" * 64,
    "rows_sha": "b" * 64,
    "layout": "day",
    "key": "intraday/v1/tw_equity_bar_1m/symbol=0050/year=2026/month=11/day=13/part.parquet",
    "etag": '"0123456789abcdef0123456789abcdef"',
}


async def _table_exists(db) -> bool:
    return await db.scalar(text("SELECT to_regclass('public.intraday_partition') IS NOT NULL"))


@pytest.mark.asyncio
async def test_intraday_index_roundtrip_validates_rows_and_blocks_a_lossy_downgrade():
    async with get_active_migration_database_factory().clone(
        "e9f4a1c6b3d5", "findb_intraday_index"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        async with engine.connect() as db:
            assert not await _table_exists(db)
        await run_alembic(url, INTRADAY_INDEX)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO dataset_registry(dataset_key,name,asset_class,market,frequency,"
                    "is_active,config,created_at,updated_at) VALUES('tw_equity_bar_1m','TW bars',"
                    "'equity','TW','minute',true,'{}',now(),now())"
                )
            )
            for invalid in (
                {"symbol": "0050/x"},  # the symbol is one segment of the object key
                {"rows": 0},  # a day without rows has no index entry
                {"sha": "A" * 64},
                {"rows_sha": "b" * 63},
                {"layout": "week"},
                {"key": ""},
                {"etag": ""},
            ):
                with pytest.raises(IntegrityError, match="intraday_partition_"):
                    async with db.begin_nested():
                        await db.execute(text(INSERT), {**VALID, **invalid})
            await db.execute(text(INSERT), VALID)
            # One row per dataset, symbol and trade date.
            with pytest.raises(IntegrityError, match="pk_intraday_partition"):
                async with db.begin_nested():
                    await db.execute(text(INSERT), VALID)

        with pytest.raises(subprocess.CalledProcessError) as blocked:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "downgrade is blocked" in blocked.value.stderr
        async with engine.begin() as db:
            assert await db.scalar(text("SELECT count(*) FROM intraday_partition")) == 1
            await db.execute(text("DELETE FROM intraday_partition"))

        await run_alembic(url, BEFORE, command="downgrade")
        async with engine.connect() as db:
            assert not await _table_exists(db)
        await run_alembic(url, INTRADAY_INDEX)
        async with engine.connect() as db:
            assert await _table_exists(db)

        # The follow-up revision only adds the index of days waiting for their merge.
        await run_alembic(url, DAY_LAYOUT_INDEX)
        async with engine.begin() as db:
            await db.execute(text(INSERT), VALID)
            await db.execute(
                text(INSERT), {**VALID, "symbol": "2330", "layout": "month", "key": "month-object"}
            )
            definition = await db.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'idx_intraday_partition_day_layout'"
                )
            )
            assert "WHERE" in definition and "'day'" in definition
            await db.execute(text("DELETE FROM intraday_partition"))
        await run_alembic(url, INTRADAY_INDEX, command="downgrade")
        async with engine.connect() as db:
            assert await _table_exists(db)
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_indexes "
                        "WHERE indexname = 'idx_intraday_partition_day_layout'"
                    )
                )
                == 0
            )
