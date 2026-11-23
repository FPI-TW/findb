"""The download quota is additive: keys keep working, an older release ignores it."""

import uuid

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "44a94f0f8481"
REVISION = "b6d8f0a2c4e7"


async def _shape(engine) -> tuple[bool, bool]:
    async with engine.connect() as db:
        column = await db.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = 'api_key'"
                " AND column_name = 'intraday_download_daily_limit'"
            )
        )
        table = await db.scalar(text("SELECT to_regclass('intraday_download_usage') IS NOT NULL"))
        return bool(column), bool(table)


@pytest.mark.asyncio
async def test_existing_keys_get_the_default_limit_and_downgrade_removes_the_quota():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "download_quota") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        key_id = uuid.uuid4()
        async with engine.begin() as db:
            # A key issued by the release before this revision.
            await db.execute(
                text(
                    "INSERT INTO api_key (key_id, key_hash, kind, owner, tier, scopes,"
                    " rate_limit_requests, rate_limit_window, page_size_limit, usage_count,"
                    " created_at) VALUES (:id, :hash, 'serve', 'research', 'standard',"
                    " '[\"serve\"]', 100, 60, 1000, 0, now())"
                ),
                {"id": key_id, "hash": "f" * 64},
            )

        await run_alembic(url, REVISION)
        assert await _shape(engine) == (True, True)
        async with engine.begin() as db:
            assert (
                await db.scalar(
                    text("SELECT intraday_download_daily_limit FROM api_key WHERE key_id = :id"),
                    {"id": key_id},
                )
                == 5000
            )
            await db.execute(
                text(
                    "INSERT INTO intraday_download_usage (key_id, usage_date, object_count,"
                    " updated_at) VALUES (:id, '2026-11-23', 3, now())"
                ),
                {"id": key_id},
            )
        with pytest.raises(Exception, match="nonnegative"):
            async with engine.begin() as db:
                await db.execute(
                    text(
                        "UPDATE api_key SET intraday_download_daily_limit = -1 WHERE key_id = :id"
                    ),
                    {"id": key_id},
                )

        # Counters only bound the current day; downgrade drops them with the column.
        await run_alembic(url, BEFORE, command="downgrade", forward_columns=False)
        assert await _shape(engine) == (False, False)
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM api_key")) == 1
