"""The HKEX feed installs its dataset and stopped control, and governs their schedule."""

import json
import subprocess
from copy import deepcopy

import pytest
from sqlalchemy import text

from scripts.preflight_catalog_schedule import migration_module
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "b7d9f1a3c5e8"
REVISION = "d8e0f2a4c6b9"
DATASET = "hk_equity_official_eod"
CONTROL = "full_market_hkex_v1"


async def _report(engine) -> dict[str, str]:
    async with engine.connect() as db:
        report, _, _ = await db.run_sync(migration_module().inspect)
    return {f"{row['kind']}:{row['key']}": row["status"] for row in report}


@pytest.mark.asyncio
async def test_the_feed_is_installed_stopped_with_the_config_seed_declares():
    async with get_active_migration_database_factory().clone("e9f4a1c6b3d5", "hkex_feed") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        # The deploy preflight runs the new inspection before the upgrade.
        assert all(status == "already_compliant" for status in (await _report(engine)).values())

        await run_alembic(url, REVISION)

        async with engine.connect() as db:
            row = (
                (
                    await db.execute(
                        text(
                            "SELECT name, asset_class, market, frequency, is_active, config "
                            "FROM dataset_registry WHERE dataset_key = :key"
                        ),
                        {"key": DATASET},
                    )
                )
                .mappings()
                .one()
            )
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
        definition = next(item for item in DATASETS if item["dataset_key"] == DATASET)
        assert {key: row[key] for key in ("name", "asset_class", "market", "frequency")} == {
            key: definition[key] for key in ("name", "asset_class", "market", "frequency")
        }
        assert row["is_active"] is True and row["config"] == definition["config"]
        assert tuple(control) == (
            "hkex",
            "full_market",
            "asia_pacific_markets_window",
            "21:05:00",
            "Asia/Hong_Kong",
            "stopped",
            "stopped",
        )
        assert scope == {DATASET}
        report = await _report(engine)
        assert report[f"dataset:{DATASET}"] == report[f"control:{CONTROL}"] == "already_compliant"
        assert all(status == "already_compliant" for status in report.values())

        # A clock this revision did not install is an unknown difference for the deploy.
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE scheduler_control SET scheduled_local_time = '21:30' "
                    "WHERE scheduler_key = :key"
                ),
                {"key": CONTROL},
            )
            config = deepcopy(definition["config"])
            config["delivery_expectation"]["missing_delivery"]["deadline_local_time"] = "23:00:00"
            await db.execute(
                text(
                    "UPDATE dataset_registry SET config = CAST(:config AS jsonb) WHERE dataset_key = :key"
                ),
                {"key": DATASET, "config": json.dumps(config)},
            )
        report = await _report(engine)
        assert report[f"dataset:{DATASET}"] == report[f"control:{CONTROL}"] == "unknown_difference"
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE scheduler_control SET scheduled_local_time = '21:05' "
                    "WHERE scheduler_key = :key"
                ),
                {"key": CONTROL},
            )
            await db.execute(
                text(
                    "UPDATE dataset_registry SET config = CAST(:config AS jsonb) WHERE dataset_key = :key"
                ),
                {"key": DATASET, "config": json.dumps(definition["config"])},
            )

        # Once the dataset has been used, a schema rollback would orphan its history.
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO ingestion_run (run_id, dataset_key, source, status, raw_records, total_records, "
                    "success_records, failed_records, created_at) "
                    "VALUES (gen_random_uuid(), :key, 'hkex', 'completed', 0, 0, 0, 0, now())"
                ),
                {"key": DATASET},
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
                    "SELECT (SELECT count(*) FROM dataset_registry WHERE dataset_key = :key) "
                    "+ (SELECT count(*) FROM scheduler_control WHERE scheduler_key = :control)"
                ),
                {"key": DATASET, "control": CONTROL},
            )
        assert remaining == 0
