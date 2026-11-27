"""The TWSE and TPEx index feeds install their datasets and two stopped controls (e28706e77614)."""

import json
import subprocess
from copy import deepcopy

import pytest
from sqlalchemy import text

from scripts.preflight_catalog_schedule import migration_module
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "e0649188f071"
REVISION = "e28706e77614"
SCOPES = {
    "full_market_twse_v1": ("twse", "tw_twse_index_eod"),
    "full_market_tpex_v1": ("tpex", "tw_tpex_index_eod"),
}
DATASETS_ADDED = tuple(dataset for _, dataset in SCOPES.values())


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
    async with get_active_migration_database_factory().clone("1587769858c0", "twse_tpex_feeds") as (
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
                            "SELECT dataset_key, name, description, asset_class, market, "
                            "frequency, is_active, config FROM dataset_registry "
                            "WHERE dataset_key IN ('tw_twse_index_eod', 'tw_tpex_index_eod')"
                        )
                    )
                ).mappings()
            }
            controls = {
                row[0]: tuple(row[1:])
                for row in (
                    await db.execute(
                        text(
                            "SELECT scheduler_key, provider, execution_profile, slot_id, "
                            "scheduled_local_time::text, timezone, desired_state, observed_state "
                            "FROM scheduler_control WHERE provider IN ('twse', 'tpex')"
                        )
                    )
                ).all()
            }
            scopes = {
                key: set(
                    await db.scalars(
                        text(
                            "SELECT dataset_key FROM scheduler_dataset WHERE scheduler_key = :key"
                        ),
                        {"key": key},
                    )
                )
                for key in SCOPES
            }
        # Exactly what seed declares, so registry provisioning has nothing to repair.
        for key in DATASETS_ADDED:
            definition = next(item for item in DATASETS if item["dataset_key"] == key)
            row = rows[key]
            fields = ("name", "description", "asset_class", "market", "frequency")
            assert {field: row[field] for field in fields} == {
                field: definition[field] for field in fields
            }
            assert (row["asset_class"], row["market"]) == ("index", "TW")
            assert row["is_active"] is True and row["config"] == definition["config"]
        # One control per exchange, each showing its only feed: 14:15 Taipei.
        assert controls == {
            key: (
                provider,
                "full_market",
                "taiwan_market_window",
                "14:15:00",
                "Asia/Taipei",
                "stopped",
                "stopped",
            )
            for key, (provider, _) in SCOPES.items()
        }
        assert scopes == {key: {dataset} for key, (_, dataset) in SCOPES.items()}
        report = await _report(engine)
        assert all(report[f"control:{key}"] == "already_compliant" for key in SCOPES)
        assert all(report[f"dataset:{key}"] == "already_compliant" for key in DATASETS_ADDED)
        assert all(status == "already_compliant" for status in report.values())

        # A clock or source this revision did not install is an unknown difference.
        twse = next(item for item in DATASETS if item["dataset_key"] == "tw_twse_index_eod")
        changed = deepcopy(twse["config"])
        changed["delivery_expectation"]["schedule"]["expected_sources"] = ["tpex"]
        await _set_config(engine, "tw_twse_index_eod", changed)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE scheduler_control SET scheduled_local_time = '17:30' "
                    "WHERE scheduler_key = 'full_market_tpex_v1'"
                )
            )
        report = await _report(engine)
        assert report["dataset:tw_twse_index_eod"] == "unknown_difference"
        assert report["control:full_market_tpex_v1"] == "unknown_difference"
        assert report["dataset:tw_tpex_index_eod"] == "already_compliant"
        assert report["control:full_market_twse_v1"] == "already_compliant"
        await _set_config(engine, "tw_twse_index_eod", twse["config"])
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE scheduler_control SET scheduled_local_time = '14:15' "
                    "WHERE scheduler_key = 'full_market_tpex_v1'"
                )
            )

        # Once a dataset has been used, a schema rollback would orphan its history.
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO ingestion_run (run_id, dataset_key, source, status, raw_records, "
                    "total_records, success_records, failed_records, created_at) "
                    "VALUES (gen_random_uuid(), 'tw_tpex_index_eod', 'tpex', 'completed', "
                    "0, 0, 0, 0, now())"
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
                    "('tw_twse_index_eod', 'tw_tpex_index_eod')) + (SELECT count(*) FROM "
                    "scheduler_control WHERE provider IN ('twse', 'tpex')) + (SELECT count(*) "
                    "FROM scheduler_dataset WHERE scheduler_key IN ('full_market_twse_v1', "
                    "'full_market_tpex_v1'))"
                )
            )
        assert remaining == 0
