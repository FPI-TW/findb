"""Slots are a catalog-derived Dashboard grouping, projected once onto stored rows."""

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import DatasetRegistry
from app.services.provider_metadata import acquisition_feeds
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "b5d8e2f4a6c1"
HEAD = "c3f6a9d1e2b7"


def _migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/c3f6a9d1e2b7_catalog_slot_projection.py"
    )
    spec = importlib.util.spec_from_file_location("catalog_slot_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_and_frozen_migration_use_catalog_slots():
    feeds = {feed.dataset_key: feed for feed in acquisition_feeds()}
    module = _migration()
    for definition in DATASETS:
        feed = feeds.get(definition["dataset_key"])
        if feed is None:
            continue
        schedule = definition["config"]["delivery_expectation"]["schedule"]
        assert schedule["slot_id"] == feed.delivery.slot_id, definition["dataset_key"]
        assert module.DATASET_SLOTS[feed.dataset_key] == feed.delivery.slot_id
        assert module.CONTROL_SLOTS[f"full_market_{feed.provider}_v1"] == feed.delivery.slot_id
    assert set(module.DATASET_SLOTS) == set(feeds)


@pytest.mark.asyncio
async def test_slot_migration_projects_catalog_slots_and_keeps_other_config():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "catalog_slots") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        hk = json.dumps(
            {
                "operator_note": "retain",
                "delivery_expectation": {
                    "schedule": {"slot_id": "taiwan_market_window", "local_time": "18:30:00"}
                },
            }
        )
        async with AsyncSession(engine) as db:
            await db.merge(
                DatasetRegistry(
                    dataset_key="hk_equity_eod",
                    name="HK",
                    market="HK",
                    asset_class="equity",
                    frequency="daily",
                    config=json.loads(hk),
                )
            )
            await db.commit()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE scheduler_control SET slot_id='taiwan_market_window' "
                    "WHERE scheduler_key='full_market_factset_v1'"
                )
            )
        await run_alembic(url, HEAD)
        async with engine.connect() as conn:
            config = (
                await conn.execute(
                    text("SELECT config FROM dataset_registry WHERE dataset_key='hk_equity_eod'")
                )
            ).scalar_one()
            slots = dict(
                (
                    await conn.execute(text("SELECT scheduler_key, slot_id FROM scheduler_control"))
                ).all()
            )
        assert config["delivery_expectation"]["schedule"] == {
            "slot_id": "asia_pacific_markets_window",
            "local_time": "18:30:00",
        }
        assert config["operator_note"] == "retain"
        for key, slot in _migration().CONTROL_SLOTS.items():
            if key in slots:
                assert slots[key] == slot
