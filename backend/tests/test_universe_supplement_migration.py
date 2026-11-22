"""The catalogue supplement table and plan key are additive; recorded ones block downgrade."""

import subprocess
import uuid

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "f3a5c7e9b1d2"
HEAD = "a4c6e8f0b2d3"


async def _shape(engine) -> tuple[bool, bool]:
    async with engine.connect() as db:
        table = await db.scalar(text("SELECT to_regclass('universe_supplement') IS NOT NULL"))
        column = await db.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE "
                "table_name = 'daily_delivery_plan' AND column_name = 'supplement_id')"
            )
        )
        return bool(table), bool(column)


@pytest.mark.asyncio
async def test_supplement_schema_is_added_and_recorded_supplements_block_downgrade():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "universe_supplement"
    ) as (url, engine):
        await run_alembic(url, HEAD)
        assert await _shape(engine) == (True, True)
        await run_alembic(url, BEFORE, command="downgrade", forward_columns=False)
        assert await _shape(engine) == (False, False)

        await run_alembic(url, HEAD)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO universe_supplement (supplement_id, dataset_key, provider,"
                    " effective_date, observed_at, member_sha256, member_count, members,"
                    " created_at) VALUES (:id, 'us_equity_eod', 'twelve_data', '2026-11-20',"
                    " now(), :sha, 1, CAST(:members AS jsonb), now())"
                ),
                {"id": uuid.uuid4(), "sha": "a" * 64, "members": '[{"symbol": "LATE"}]'},
            )
        with pytest.raises(subprocess.CalledProcessError) as refused:
            await run_alembic(url, BEFORE, command="downgrade", forward_columns=False)
        assert "roll forward" in refused.value.stderr
        assert await _shape(engine) == (True, True)
