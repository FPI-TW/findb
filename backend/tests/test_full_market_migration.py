"""Full-market schema upgrade/downgrade roundtrip in an isolated database."""

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory
from tests.test_tw_minute_migration import _run_alembic


@pytest.mark.asyncio
async def test_migration_upgrade_downgrade_upgrade_isolated():
    async with get_active_migration_database_factory().clone(
        "a8b9c0d1e2f3", "full_market_roundtrip"
    ) as (url, engine):
        await _run_alembic(url, "b9c0d1e2f3a4")
        await _run_alembic(url, "1331cb73adad")

        async with engine.connect() as connection:
            assert await connection.scalar(
                text("SELECT to_regclass('futures_contract_eod') IS NOT NULL")
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns WHERE table_name='daily_delivery_member' AND column_name='outcome_history'"
                    )
                )
                == 1
            )
        await _run_alembic(url, "b9c0d1e2f3a4", "downgrade")
        async with engine.connect() as connection:
            assert await connection.scalar(
                text("SELECT to_regclass('futures_contract_eod') IS NULL")
            )
            assert await connection.scalar(
                text("SELECT to_regclass('market_data_minute') IS NOT NULL")
            )
        await _run_alembic(url, "1331cb73adad")
