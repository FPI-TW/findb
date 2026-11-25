"""The dollar index feed joins the FMP control with the config seed declares, and governs its clock."""

import json
import subprocess
from copy import deepcopy

import pytest
from sqlalchemy import text

from scripts.preflight_catalog_schedule import migration_module
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "2bbe07b98293"
REVISION = "64da893e5e78"
DATASET = "us_dollar_index_eod"
CONTROL = "full_market_fmp_v1"
PREVIOUS_SCOPE = ["hk_index_eod", "us_index_eod"]
STAGED_SCOPE = ["hk_index_eod", "us_dollar_index_eod", "us_index_eod"]
CLIENT = (
    "INSERT INTO source_client(client_id,name,source_name,key_hash,allowed_datasets,"
    "rate_limit_requests,rate_limit_window,usage_count,created_at,updated_at,revoked_at) "
    "VALUES(gen_random_uuid(),:name,'fmp',:hash,CAST(:scope AS jsonb),100,60,0,now(),now(),"
    "CASE WHEN :revoked THEN now() END)"
)


async def _scopes(engine) -> dict[str, list[str]]:
    async with engine.connect() as db:
        rows = await db.execute(text("SELECT name, allowed_datasets FROM source_client"))
        return {name: scope for name, scope in rows.all()}


async def _report(engine) -> dict[str, str]:
    async with engine.connect() as db:
        report, _, _ = await db.run_sync(migration_module().inspect)
    return {f"{row['kind']}:{row['key']}": row["status"] for row in report}


async def _set_config(engine, config: dict) -> None:
    async with engine.begin() as db:
        await db.execute(
            text(
                "UPDATE dataset_registry SET config = CAST(:config AS jsonb) WHERE dataset_key = :key"
            ),
            {"key": DATASET, "config": json.dumps(config)},
        )


def test_the_preflight_inspects_this_revision():
    module = migration_module()
    assert module.revision == REVISION
    assert module.FEEDS[DATASET] == ("fmp", "America/New_York", "17:15:00", (), None, 0)
    # The earlier feeds keep the rules of the revisions that installed them.
    assert {"hk_index_eod", "us_index_eod", "us_equity_eod"} <= set(module.FEEDS)


@pytest.mark.asyncio
async def test_the_feed_joins_the_stopped_fmp_control_with_the_config_seed_declares():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "fmp_dollar") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        # The FMP Fetcher's credential, an operator-widened one, and a revoked one.
        async with engine.begin() as db:
            for name, scope, revoked in (
                ("fetcher-fmp", PREVIOUS_SCOPE, False),
                ("fmp-operator", [*PREVIOUS_SCOPE, "hk_equity_eod"], False),
                ("fmp-revoked", PREVIOUS_SCOPE, True),
            ):
                await db.execute(
                    text(CLIENT),
                    {
                        "name": name,
                        "hash": name.ljust(64, "0"),
                        "scope": json.dumps(scope),
                        "revoked": revoked,
                    },
                )
        # The deploy preflight runs the new inspection before the upgrade.
        assert all(status == "already_compliant" for status in (await _report(engine)).values())

        await run_alembic(url, REVISION)

        async with engine.connect() as db:
            row = (
                (
                    await db.execute(
                        text(
                            "SELECT name, description, asset_class, market, frequency, is_active, "
                            "config FROM dataset_registry WHERE dataset_key = :key"
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
                        "SELECT scheduled_local_time::text, timezone, desired_state "
                        "FROM scheduler_control WHERE scheduler_key = :key"
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
        assert {
            field: row[field] for field in ("name", "description", "asset_class", "market")
        } == {
            field: definition[field] for field in ("name", "description", "asset_class", "market")
        }
        assert row["frequency"] == definition["frequency"] == "daily"
        assert row["is_active"] is True and row["config"] == definition["config"]
        # The control still shows its earliest feed (08:30 Taipei) and stays stopped.
        assert tuple(control) == ("08:30:00", "Asia/Taipei", "stopped")
        assert scope == {"hk_index_eod", "us_index_eod", DATASET}
        # Only the credential still at exactly the two index datasets is widened.
        assert await _scopes(engine) == {
            "fetcher-fmp": STAGED_SCOPE,
            "fmp-operator": [*PREVIOUS_SCOPE, "hk_equity_eod"],
            "fmp-revoked": PREVIOUS_SCOPE,
        }
        report = await _report(engine)
        assert report[f"dataset:{DATASET}"] == "already_compliant"
        assert all(status == "already_compliant" for status in report.values())

        # A clock this revision did not install is an unknown difference for the deploy.
        changed = deepcopy(definition["config"])
        changed["delivery_expectation"]["schedule"]["local_time"] = "20:15:00"
        await _set_config(engine, changed)
        assert (await _report(engine))[f"dataset:{DATASET}"] == "unknown_difference"
        await _set_config(engine, definition["config"])

        # Once the dataset has been used, a schema rollback would orphan its history.
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO ingestion_run (run_id, dataset_key, source, status, raw_records, "
                    "total_records, success_records, failed_records, created_at) "
                    "VALUES (gen_random_uuid(), :key, 'fmp', 'completed', 0, 0, 0, 0, now())"
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
                    "+ (SELECT count(*) FROM scheduler_dataset WHERE dataset_key = :key)"
                ),
                {"key": DATASET},
            )
            control_left = await db.scalar(
                text("SELECT count(*) FROM scheduler_control WHERE scheduler_key = :key"),
                {"key": CONTROL},
            )
        # Only this revision's rows go; the FMP control and its index feeds stay.
        assert remaining == 0 and control_left == 1
        assert (await _scopes(engine))["fetcher-fmp"] == PREVIOUS_SCOPE
