"""Provider work and owners gain a lane; downgrade refuses once a lane is used (36bfabaae072)."""

import subprocess

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "7733f9692fe6"
LANES = "36bfabaae072"


@pytest.mark.asyncio
async def test_existing_owners_join_the_default_lane_and_downgrade_refuses_a_used_lane():
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "provider_work_lanes"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO provider_work_owner(environment,provider,generation) "
                    "VALUES ('local','finlab',4)"
                )
            )

        await run_alembic(url, LANES)
        async with engine.begin() as db:
            rows = (
                await db.execute(text("SELECT provider, lane, generation FROM provider_work_owner"))
            ).all()
            assert [tuple(row) for row in rows] == [("finlab", "default", 4)]
            # A second lane of the same provider is its own owner row.
            await db.execute(
                text(
                    "INSERT INTO provider_work_owner(environment,provider,lane,generation) "
                    "VALUES ('local','finlab','intraday',0)"
                )
            )
            with pytest.raises(Exception, match="lane_valid"):
                async with db.begin_nested():
                    await db.execute(
                        text(
                            "INSERT INTO provider_work_owner(environment,provider,lane,generation) "
                            "VALUES ('local','finlab','Bad-Lane',0)"
                        )
                    )

        with pytest.raises(subprocess.CalledProcessError) as refused:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "non-default lane" in refused.value.stderr

        async with engine.begin() as db:
            await db.execute(text("DELETE FROM provider_work_owner WHERE lane = 'intraday'"))
        await run_alembic(url, BEFORE, command="downgrade")
        async with engine.connect() as db:
            columns = (
                await db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'provider_work_owner'"
                    )
                )
            ).scalars()
            assert "lane" not in set(columns)
