"""Frozen clock migration, unknown-difference atomicity and preserved evidence."""

import json
import subprocess
from copy import deepcopy
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import DatasetRegistry, SchedulerControl
from scripts.preflight_catalog_schedule import migration_module
from scripts.seed_data import DATASETS, seed_datasets
from tests.migration_database import get_active_migration_database_factory, run_alembic
from tests.test_full_market import _activated_plan

BEFORE = "d8e3f0b5a2c4"
HEAD = "e9f4a1c6b3d5"


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_schedule_migration_is_atomic_and_preserves_frozen_work(unknown):
    module = migration_module()
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "catalog_clocks") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        async with AsyncSession(engine, expire_on_commit=False) as db:

            async def existing_dataset(session, key):
                dataset = await session.get(DatasetRegistry, key)
                dataset.config = deepcopy(
                    next(d["config"] for d in DATASETS if d["dataset_key"] == key)
                )
                await session.commit()
                return dataset

            with patch("tests.test_full_market._dataset", existing_dataset):
                await _activated_plan(db, "tw_etf_eod", count=1)
            for definition in DATASETS:
                dataset = await db.get(DatasetRegistry, definition["dataset_key"])
                if dataset is None:
                    dataset = DatasetRegistry(**deepcopy(definition))
                    db.add(dataset)
                if dataset.dataset_key not in module.FEEDS:
                    continue
                fields = module.FEEDS[dataset.dataset_key]
                config = deepcopy(definition["config"])
                config["operator_note"] = "retain"
                expectation = config["delivery_expectation"]
                expectation["schedule"].update(
                    timezone="Asia/Taipei",
                    local_time=fields[4],
                    target_date_lag_days=fields[5],
                    expected_sources=[fields[3]],
                )
                expectation["missing_delivery"].pop("deadline_timezone", None)
                expectation["missing_delivery"].pop("deadline_day_offset", None)
                dataset.config = config
            await db.flush()
            # Keep running state, revision and frozen first dates; only clock changes.
            control = await db.get(SchedulerControl, "full_market_finlab_v1")
            revision = control.revision
            if unknown:
                dataset = await db.get(DatasetRegistry, "us_equity_eod")
                config = deepcopy(dataset.config)
                config["delivery_expectation"]["schedule"]["local_time"] = "12:34:00"
                dataset.config = config
            await db.commit()
            before_configs = dict(
                (
                    await db.execute(select(DatasetRegistry.dataset_key, DatasetRegistry.config))
                ).all()
            )
        async with engine.connect() as db:
            frozen = (
                (await db.execute(text("SELECT row_to_json(p) FROM daily_delivery_plan p")))
                .scalars()
                .all()
            )
            states = (
                (await db.execute(text("SELECT row_to_json(p) FROM full_market_dataset_state p")))
                .scalars()
                .all()
            )
            report, _, _ = await db.run_sync(module.inspect)
            assert any(
                row["status"] == ("unknown_difference" if unknown else "known_old_default")
                for row in report
            )
        if unknown:
            with pytest.raises(subprocess.CalledProcessError) as failed:
                await run_alembic(url, HEAD)
            assert "unknown differences" in failed.value.stderr
        else:
            try:
                await run_alembic(url, HEAD)
            except subprocess.CalledProcessError as exc:
                pytest.fail(exc.stderr)
        async with engine.connect() as db:
            assert (
                await db.execute(text("SELECT row_to_json(p) FROM daily_delivery_plan p"))
            ).scalars().all() == frozen
            assert (
                await db.execute(text("SELECT row_to_json(p) FROM full_market_dataset_state p"))
            ).scalars().all() == states
            if unknown:
                after = dict(
                    (
                        await db.execute(text("SELECT dataset_key, config FROM dataset_registry"))
                    ).all()
                )
                assert after == before_configs
                assert await db.scalar(text("SELECT version_num FROM alembic_version")) == BEFORE
            else:
                report, _, _ = await db.run_sync(module.inspect)
                assert all(row["status"] == "already_compliant" for row in report)
        if not unknown:
            async with AsyncSession(engine, expire_on_commit=False) as db:
                await seed_datasets(db)
                first = dict(
                    (
                        await db.execute(
                            select(DatasetRegistry.dataset_key, DatasetRegistry.config)
                        )
                    ).all()
                )
                await seed_datasets(db)
                assert (
                    dict(
                        (
                            await db.execute(
                                select(DatasetRegistry.dataset_key, DatasetRegistry.config)
                            )
                        ).all()
                    )
                    == first
                )
                control = await db.get(SchedulerControl, "full_market_finlab_v1")
                assert (control.desired_state, control.revision) == ("running", revision)
                assert first["us_equity_eod"]["operator_note"] == "retain"
                assert (
                    first["us_equity_eod"]["delivery_expectation"]["schedule"]["local_time"]
                    == "17:00:00"
                )


def test_frozen_migration_matches_live_catalog_projection():
    module = migration_module()
    for definition in DATASETS:
        if definition["dataset_key"] in module.FEEDS:
            status, _ = module.project_config(definition["dataset_key"], definition["config"])
            assert status == "already_compliant"


def test_unknown_deadline_timezone_and_clock_are_rejected():
    module = migration_module()
    config = deepcopy(next(d["config"] for d in DATASETS if d["dataset_key"] == "us_equity_eod"))
    for field, value in (
        ("deadline_timezone", "America/New_York"),
        ("deadline_local_time", "09:00:00"),
        ("deadline_day_offset", 2),
        ("deadline_day_offset", True),
    ):
        changed = deepcopy(config)
        changed["delivery_expectation"]["missing_delivery"][field] = value
        assert module.project_config("us_equity_eod", changed)[0] == "unknown_difference"
        assert json.dumps(changed) == json.dumps(module.project_config("us_equity_eod", changed)[1])


@pytest.mark.asyncio
async def test_clean_migration_chain():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "catalog_bootstrap"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        await run_alembic(url, HEAD)


@pytest.mark.asyncio
async def test_schedule_downgrade_requires_forward_fix():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "catalog_floor") as (
        url,
        engine,
    ):
        await run_alembic(url, HEAD)
        with pytest.raises(subprocess.CalledProcessError) as failed:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "forward fix" in failed.value.stderr
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
