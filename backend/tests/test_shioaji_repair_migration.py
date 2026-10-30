"""Expand-only approval schema preserves all pre-existing frozen work."""

import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.migration_database import get_active_migration_database_factory, run_alembic
from tests.test_full_market import _activated_plan


def current_dataset_fixture(monkeypatch):
    from copy import deepcopy

    from app.models.registry import DatasetRegistry
    from scripts.seed_data import DATASETS
    from tests import test_full_market

    async def existing(db, key):
        dataset = await db.get(DatasetRegistry, key)
        dataset.config = deepcopy(
            next(row["config"] for row in DATASETS if row["dataset_key"] == key)
        )
        await db.commit()
        return dataset

    monkeypatch.setattr(test_full_market, "_dataset", existing)


@pytest.mark.asyncio
async def test_repair_schema_roundtrip_preserves_frozen_scope(monkeypatch):
    current_dataset_fixture(monkeypatch)
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "shioaji_repair") as (
        url,
        engine,
    ):
        await run_alembic(url, "1e68a0c9b3d2")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            await _activated_plan(db)
        async with engine.connect() as db:
            before = {
                table: (await db.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY 1")))
                .scalars()
                .all()
                for table in (
                    "universe_release",
                    "universe_member",
                    "daily_delivery_plan",
                    "daily_delivery_member",
                )
            }
        await run_alembic(url, "head")
        async with engine.connect() as db:
            for table, rows in before.items():
                assert (
                    await db.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY 1"))
                ).scalars().all() == rows
            assert await db.scalar(text("SELECT count(*) FROM shioaji_member_repair")) == 0
        await run_alembic(url, "1e68a0c9b3d2", command="downgrade")
        await run_alembic(url, "head")
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM shioaji_repair_ack")) == 0


@pytest.mark.asyncio
async def test_repair_evidence_blocks_schema_downgrade(monkeypatch):
    current_dataset_fixture(monkeypatch)
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "shioaji_repair_floor"
    ) as (
        url,
        engine,
    ):
        await run_alembic(url, "head")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            from tests.test_shioaji_repair import authorize, proposal, setup

            work, member, _, _, _ = await setup(db)
            await authorize(db, work, proposal(member))
        with pytest.raises(subprocess.CalledProcessError) as blocked:
            await run_alembic(url, "1e68a0c9b3d2", command="downgrade")
        assert "repair evidence must be retained" in blocked.value.stderr
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM shioaji_member_repair")) == 1
