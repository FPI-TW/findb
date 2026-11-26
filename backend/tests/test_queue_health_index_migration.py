"""Retry-exhausted runs get a partial index, and downgrade removes only that index."""

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

PREVIOUS = "c7e9a1b3d5f8"
REVISION = "208a9707fe3f"


async def _index(engine) -> str | None:
    async with engine.connect() as connection:
        return await connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = 'ingestion_run' AND indexname = 'idx_run_retry_exhausted'"
            )
        )


@pytest.mark.asyncio
async def test_upgrade_indexes_retry_exhausted_runs_and_downgrade_drops_it():
    async with get_active_migration_database_factory().clone("1587769858c0", "retry_index") as (
        url,
        engine,
    ):
        await run_alembic(url, PREVIOUS)
        assert await _index(engine) is None

        await run_alembic(url, REVISION)
        assert await _index(engine) == (
            "CREATE INDEX idx_run_retry_exhausted ON public.ingestion_run USING btree "
            "(failure_code) WHERE ((failure_code)::text = 'RETRY_EXHAUSTED'::text)"
        )

        await run_alembic(url, PREVIOUS, command="downgrade")
        assert await _index(engine) is None
