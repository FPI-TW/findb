"""The FMP index feeds install their datasets and one stopped control, and govern their schedule."""

import json
import subprocess
from copy import deepcopy

import pytest
from sqlalchemy import text

from scripts.preflight_catalog_schedule import migration_module
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "d8e0f2a4c6b9"
REVISION = "e5a7c9b1d3f2"
DATASETS_ADDED = ("hk_index_eod", "us_index_eod")
CONTROL = "full_market_fmp_v1"


async def _report(engine) -> dict[str, str]:
    async with engine.connect() as db:
        report, _, _ = await db.run_sync(migration_module().inspect)
    return {f"{row['kind']}:{row['key']}": row["status"] for row in report}


async def _set_config(engine, key: str, config: dict) -> None:
    async with engine.begin() as db:
        await db.execute(
            text(
                "UPDATE dataset_registry SET config = CAST(:config AS jsonb) WHERE dataset_key = :key"
            ),
            {"key": key, "config": json.dumps(config)},
        )


@pytest.mark.asyncio
async def test_the_feeds_are_installed_stopped_with_the_config_seed_declares():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "fmp_feeds") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        # The deploy preflight runs the new inspection before the upgrade.
        assert all(status == "already_compliant" for status in (await _report(engine)).values())

        await run_alembic(url, REVISION)

        async with engine.connect() as db:
            rows = {
                row["dataset_key"]: row
                for row in (
                    await db.execute(
                        text(
                            "SELECT dataset_key, name, asset_class, market, frequency, is_active, "
                            "config FROM dataset_registry WHERE dataset_key IN ('hk_index_eod', "
                            "'us_index_eod')"
                        )
                    )
                ).mappings()
            }
            control = (
                await db.execute(
                    text(
                        "SELECT provider, execution_profile, slot_id, scheduled_local_time::text, "
                        "timezone, desired_state, observed_state FROM scheduler_control "
                        "WHERE scheduler_key = :key"
                    ),
                    {"key": CONTROL},
                )
            ).one()
            scope = set(
                await db.scalars(
                    text("SELECT dataset_key FROM scheduler_dataset WHERE scheduler_key = :key"),
                    {"key": CONTROL},
                )
            )
        # Exactly what seed declares, so registry provisioning has nothing to repair.
        for key in DATASETS_ADDED:
            definition = next(item for item in DATASETS if item["dataset_key"] == key)
            row = rows[key]
            assert {
                field: row[field] for field in ("name", "asset_class", "market", "frequency")
            } == {
                field: definition[field] for field in ("name", "asset_class", "market", "frequency")
            }
            assert row["asset_class"] == "index"
            assert row["is_active"] is True and row["config"] == definition["config"]
        # The control shows its earliest feed: 08:30 Taipei, the New York group.
        assert tuple(control) == (
            "fmp",
            "full_market",
            "western_markets_window",
            "08:30:00",
            "Asia/Taipei",
            "stopped",
            "stopped",
        )
        assert scope == set(DATASETS_ADDED)
        report = await _report(engine)
        assert report[f"control:{CONTROL}"] == "already_compliant"
        assert all(report[f"dataset:{key}"] == "already_compliant" for key in DATASETS_ADDED)
        assert all(status == "already_compliant" for status in report.values())

        # A clock or lag this revision did not install is an unknown difference for the deploy.
        us = next(item for item in DATASETS if item["dataset_key"] == "us_index_eod")["config"]
        changed = deepcopy(us)
        changed["delivery_expectation"]["schedule"]["target_date_lag_days"] = 0
        await _set_config(engine, "us_index_eod", changed)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE scheduler_control SET timezone = 'Asia/Hong_Kong', "
                    "scheduled_local_time = '20:00' WHERE scheduler_key = :key"
                ),
                {"key": CONTROL},
            )
        report = await _report(engine)
        assert (
            report["dataset:us_index_eod"] == report[f"control:{CONTROL}"] == ("unknown_difference")
        )
        assert report["dataset:hk_index_eod"] == "already_compliant"
        await _set_config(engine, "us_index_eod", us)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE scheduler_control SET timezone = 'Asia/Taipei', "
                    "scheduled_local_time = '08:30' WHERE scheduler_key = :key"
                ),
                {"key": CONTROL},
            )

        # Once a dataset has been used, a schema rollback would orphan its history.
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO ingestion_run (run_id, dataset_key, source, status, raw_records, "
                    "total_records, success_records, failed_records, created_at) "
                    "VALUES (gen_random_uuid(), 'hk_index_eod', 'fmp', 'completed', 0, 0, 0, 0, now())"
                )
            )
        with pytest.raises(subprocess.CalledProcessError) as blocked:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "downgrade is blocked" in blocked.value.stderr
        async with engine.begin() as db:
            await db.execute(text("DELETE FROM ingestion_run"))

        await run_alembic(url, BEFORE, command="downgrade")
        async with engine.connect() as db:
            remaining = await db.scalar(
                text(
                    "SELECT (SELECT count(*) FROM dataset_registry WHERE dataset_key IN "
                    "('hk_index_eod', 'us_index_eod')) + (SELECT count(*) FROM scheduler_control "
                    "WHERE scheduler_key = :control) + (SELECT count(*) FROM scheduler_dataset "
                    "WHERE scheduler_key = :control)"
                ),
                {"control": CONTROL},
            )
        assert remaining == 0
