"""FactSet HK precedence after the cutover gate retires: only the Twelve Data-only list moves."""

from copy import deepcopy

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import DatasetRegistry
from scripts.seed_data import seed_datasets
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "b3d5f7a9c1e2"
HEAD = "c4e6a8b0d2f4"
CUTOVER = {
    "provider": "factset",
    "previous_provider": "twelve_data",
    "effective_date": "2026-10-21",
}
FACTSET_FIRST = {"factset": -1, "twelve_data": 0}


async def _hk_config(engine, **changes) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await seed_datasets(db)
        dataset = await db.get(DatasetRegistry, "hk_equity_eod")
        config = deepcopy(dataset.config)
        config.pop("hk_provider_cutover", None)
        config.update(changes)
        dataset.config = config
        await db.commit()


async def _precedence(engine):
    async with engine.connect() as db:
        return await db.scalar(
            text(
                "SELECT config->'source_precedence' FROM dataset_registry "
                "WHERE dataset_key='hk_equity_eod'"
            )
        )


@pytest.mark.asyncio
async def test_never_cutover_environment_gets_factset_first_and_downgrade_restores_it():
    async with get_active_migration_database_factory().clone("e9f4a1c6b3d5", "hk_precedence") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        await _hk_config(engine, source_precedence=["twelve_data"])
        await run_alembic(url, HEAD)
        assert await _precedence(engine) == FACTSET_FIRST
        await run_alembic(url, BEFORE, command="downgrade")
        assert await _precedence(engine) == ["twelve_data"]


@pytest.mark.asyncio
async def test_cutover_value_and_other_policy_are_kept():
    async with get_active_migration_database_factory().clone(
        "e9f4a1c6b3d5", "hk_precedence_kept"
    ) as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        # An environment that applied the retired cutover keeps its value both ways.
        await _hk_config(engine, source_precedence=FACTSET_FIRST, hk_provider_cutover=CUTOVER)
        await run_alembic(url, HEAD)
        assert await _precedence(engine) == FACTSET_FIRST
        await run_alembic(url, BEFORE, command="downgrade")
        assert await _precedence(engine) == FACTSET_FIRST
        # Any other shape is operator policy.
        await _hk_config(engine, source_precedence=["factset"])
        await run_alembic(url, HEAD)
        assert await _precedence(engine) == ["factset"]
