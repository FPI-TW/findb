"""Runs are indexed by delivery part, and downgrade removes only that index."""

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

PREVIOUS = "64da893e5e78"
REVISION = "1587769858c0"


async def _index(engine) -> str | None:
    async with engine.connect() as connection:
        return await connection.scalar(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = 'ingestion_run' AND indexname = 'idx_run_delivery_part'"
            )
        )


@pytest.mark.asyncio
async def test_upgrade_indexes_delivery_part_and_downgrade_drops_it():
    async with get_active_migration_database_factory().clone("e9f4a1c6b3d5", "run_part_index") as (
        url,
        engine,
    ):
        await run_alembic(url, PREVIOUS)
        assert await _index(engine) is None

        await run_alembic(url, REVISION)
        assert await _index(engine) == (
            "CREATE INDEX idx_run_delivery_part ON public.ingestion_run "
            "USING btree (delivery_part_id)"
        )

        await run_alembic(url, PREVIOUS, command="downgrade")
        assert await _index(engine) is None
