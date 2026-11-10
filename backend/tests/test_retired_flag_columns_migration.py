"""The retired full-market flags get defaults, then leave the schema in a later release."""

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

DEFAULTS = "d7a2c4e6f8b1"
DROPPED = "e8b3d5f7a9c2"
COLUMNS = {("full_market_environment", "enabled"), ("full_market_enrollment", "reported_enabled")}


async def _columns(engine) -> dict[tuple[str, str], str | None]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT table_name, column_name, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND (table_name, column_name) IN "
                "(('full_market_environment', 'enabled'), "
                "('full_market_enrollment', 'reported_enabled'))"
            )
        )
        return {(row.table_name, row.column_name): row.column_default for row in rows}


@pytest.mark.asyncio
async def test_drop_keeps_environment_lock_and_downgrade_restores_defaults():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "retired_flags") as (
        url,
        engine,
    ):
        await run_alembic(url, DEFAULTS)
        assert await _columns(engine) == {
            ("full_market_environment", "enabled"): "false",
            ("full_market_enrollment", "reported_enabled"): "true",
        }
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO full_market_environment (environment, enabled, updated_at) "
                    "VALUES ('staging', true, now())"
                )
            )

        await run_alembic(url, DROPPED)
        assert await _columns(engine) == {}
        async with engine.connect() as connection:
            # The environment lock row itself survives the drop.
            assert (
                await connection.scalar(text("SELECT environment FROM full_market_environment"))
                == "staging"
            )

        await run_alembic(url, DEFAULTS, command="downgrade")
        restored = await _columns(engine)
        assert set(restored) == COLUMNS
        assert restored[("full_market_environment", "enabled")] == "false"
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT enabled FROM full_market_environment"))
                is False
            )
