"""The HSIL reference feeds join the HKEX control and widen its credential (21e8f89c680b)."""

import json
import subprocess
from copy import deepcopy

import pytest
from sqlalchemy import text

from scripts.preflight_catalog_schedule import migration_module
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "54a916bdb18f"
REVISION = "21e8f89c680b"
DATASETS_ADDED = ("hk_index_changes", "hk_index_constituents")
QUOTES = "hk_equity_official_eod"
CLIENT = (
    "INSERT INTO source_client(client_id,name,source_name,key_hash,allowed_datasets,"
    "rate_limit_requests,rate_limit_window,usage_count,created_at,updated_at,revoked_at) "
    "VALUES(gen_random_uuid(),:name,'hkex',:hash,CAST(:scope AS jsonb),100,60,0,now(),now(),"
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


@pytest.mark.asyncio
async def test_the_feeds_join_the_hkex_control_with_the_config_seed_declares():
    async with get_active_migration_database_factory().clone("1587769858c0", "hsil_feeds") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        async with engine.begin() as db:
            for name, scope, revoked in (
                ("fetcher-hkex", [QUOTES], False),
                ("operator-hkex", [QUOTES, "hk_equity_eod"], False),
                ("retired-hkex", [QUOTES], True),
            ):
                await db.execute(
                    text(CLIENT),
                    {"name": name, "hash": name, "scope": json.dumps(scope), "revoked": revoked},
                )
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
                            "WHERE dataset_key IN ('hk_index_constituents', 'hk_index_changes')"
                        )
                    )
                ).mappings()
            }
            bound = set(
                await db.scalars(
                    text(
                        "SELECT dataset_key FROM scheduler_dataset "
                        "WHERE scheduler_key = 'full_market_hkex_v1'"
                    )
                )
            )
            control = (
                await db.execute(
                    text(
                        "SELECT count(*), min(scheduled_local_time::text) FROM scheduler_control "
                        "WHERE provider = 'hkex'"
                    )
                )
            ).one()
        # Exactly what seed declares, so registry provisioning has nothing to repair.
        for key in DATASETS_ADDED:
            definition = next(item for item in DATASETS if item["dataset_key"] == key)
            row = rows[key]
            fields = ("name", "description", "asset_class", "market", "frequency")
            assert {field: row[field] for field in fields} == {
                field: definition[field] for field in fields
            }
            assert (row["asset_class"], row["market"]) == ("index", "HK")
            assert row["is_active"] is True and row["config"] == definition["config"]
        # One HKEX control, still 21:05, now holding all three feeds.
        assert bound == {QUOTES, *DATASETS_ADDED}
        assert tuple(control) == (1, "21:05:00")
        # Only the Fetcher's exact quotation scope is widened; operators' scopes stay.
        assert await _scopes(engine) == {
            "fetcher-hkex": [QUOTES, "hk_index_changes", "hk_index_constituents"],
            "operator-hkex": [QUOTES, "hk_equity_eod"],
            "retired-hkex": [QUOTES],
        }
        report = await _report(engine)
        assert all(report[f"dataset:{key}"] == "already_compliant" for key in DATASETS_ADDED)
        assert all(status == "already_compliant" for status in report.values())

        # A clock this revision did not install is an unknown difference.
        definition = next(item for item in DATASETS if item["dataset_key"] == "hk_index_changes")
        changed = deepcopy(definition["config"])
        changed["delivery_expectation"]["schedule"]["local_time"] = "18:00:00"
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE dataset_registry SET config = CAST(:config AS jsonb) "
                    "WHERE dataset_key = 'hk_index_changes'"
                ),
                {"config": json.dumps(changed)},
            )
        report = await _report(engine)
        assert report["dataset:hk_index_changes"] == "unknown_difference"
        assert report["dataset:hk_index_constituents"] == "already_compliant"
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE dataset_registry SET config = CAST(:config AS jsonb) "
                    "WHERE dataset_key = 'hk_index_changes'"
                ),
                {"config": json.dumps(definition["config"])},
            )

        # Once a dataset has been used, a schema rollback would orphan its history.
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO ingestion_run (run_id, dataset_key, source, status, raw_records, "
                    "total_records, success_records, failed_records, created_at) "
                    "VALUES (gen_random_uuid(), 'hk_index_constituents', 'hkex', 'completed', "
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
                    "('hk_index_constituents', 'hk_index_changes')) + (SELECT count(*) FROM "
                    "scheduler_dataset WHERE dataset_key IN "
                    "('hk_index_constituents', 'hk_index_changes'))"
                )
            )
        assert remaining == 0
        assert (await _scopes(engine))["fetcher-hkex"] == [QUOTES]
