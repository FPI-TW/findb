"""The exchange quote tables are additive; archived reports block their removal."""

import subprocess

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "c4e6a8b0d2f4"
REVISION = "e1a3c5e7f9b2"
TABLES = {"exchange_report_archive", "exchange_daily_quote"}


async def _tables(engine) -> set[str]:
    async with engine.connect() as db:
        return set(
            await db.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE 'exchange_%'"
                )
            )
        )


@pytest.mark.asyncio
async def test_tables_are_added_and_removed_only_while_no_report_is_archived():
    async with get_active_migration_database_factory().clone("e9f4a1c6b3d5", "exchange_quote") as (
        url,
        engine,
    ):
        await run_alembic(url, REVISION)
        assert await _tables(engine) == TABLES
        await run_alembic(url, BEFORE, command="downgrade")
        assert await _tables(engine) == set()

        await run_alembic(url, REVISION)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO exchange_report_archive (archive_id, source, board, trade_date, "
                    "gzip_sha256, gzip_bytes, original_sha256, original_bytes, source_url, "
                    "source_last_modified, source_etag, object_key, object_etag, created_at) "
                    "VALUES (gen_random_uuid(), 'hkex', 'main', '2026-11-20', :sha, 1, :sha, 1, "
                    "'https://www.hkex.com.hk/eng/stat/smstat/dayquot/d261120e.htm', now(), "
                    "'\"etag\"', 'archive/v1/hkex/report.htm.gz', '\"etag\"', now())"
                ),
                {"sha": "a" * 64},
            )
        with pytest.raises(subprocess.CalledProcessError) as blocked:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "downgrade is blocked" in blocked.value.stderr
        assert await _tables(engine) == TABLES
