"""Staged descriptions name the current provider and contract version; other text is kept."""

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "e5a7c9b1d3f2"
REVISION = "44a94f0f8481"
CUSTOM = "Operator note: FMP index feed"


async def _descriptions(engine) -> dict[str, str]:
    async with engine.connect() as db:
        rows = await db.execute(text("SELECT dataset_key, description FROM dataset_registry"))
        return {row.dataset_key: row.description for row in rows}


@pytest.mark.asyncio
async def test_only_the_descriptions_written_by_migrations_or_seed_are_corrected():
    async with get_active_migration_database_factory().clone("e9f4a1c6b3d5", "descriptions") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        before = await _descriptions(engine)
        # The values staging and production hold, as earlier revisions wrote them.
        assert before["hk_equity_eod"] == "Staged twelve_data market_eod.v1 full-market daily feed"
        assert before["hk_index_eod"] == "Staged fmp market_eod.v1 full-market daily feed"
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE dataset_registry SET description = :d WHERE dataset_key = :k"),
                {"d": CUSTOM, "k": "us_index_eod"},
            )

        await run_alembic(url, REVISION)
        after = await _descriptions(engine)
        assert after["hk_equity_eod"] == "Staged factset market_eod.v2 full-market daily feed"
        assert after["hk_index_eod"] == "Staged fmp market_eod.v2 full-market daily feed"
        assert after["us_index_eod"] == CUSTOM
        unchanged = set(before) - {"hk_equity_eod", "hk_index_eod", "us_index_eod"}
        assert {key: after[key] for key in unchanged} == {key: before[key] for key in unchanged}

        # Display text only: downgrade keeps the correction.
        await run_alembic(url, BEFORE, command="downgrade")
        assert await _descriptions(engine) == after


@pytest.mark.asyncio
async def test_a_locally_seeded_hong_kong_description_is_corrected():
    async with get_active_migration_database_factory().clone("e9f4a1c6b3d5", "seeded_hk") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE dataset_registry SET description = :d WHERE dataset_key = :k"),
                {"d": "Staged factset market_eod.v1 full-market daily feed", "k": "hk_equity_eod"},
            )
        await run_alembic(url, REVISION)
        assert (await _descriptions(engine))["hk_equity_eod"] == (
            "Staged factset market_eod.v2 full-market daily feed"
        )
