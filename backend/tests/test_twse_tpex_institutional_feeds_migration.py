"""The TWSE and TPEx institutional feeds join their controls and widen scopes (54a916bdb18f)."""

import hashlib
import json
import subprocess
from copy import deepcopy

import pytest
from sqlalchemy import text

from scripts.preflight_catalog_schedule import migration_module
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "e28706e77614"
REVISION = "54a916bdb18f"
SCOPES = {
    "full_market_twse_v1": {
        "tw_twse_index_eod",
        "tw_twse_institutional_stock",
        "tw_twse_institutional_summary",
    },
    "full_market_tpex_v1": {
        "tw_tpex_index_eod",
        "tw_tpex_institutional_stock",
        "tw_tpex_institutional_summary",
    },
}
DATASETS_ADDED = sorted(key for scope in SCOPES.values() for key in scope if "institutional" in key)


async def _report(engine) -> dict[str, str]:
    async with engine.connect() as db:
        report, _, _ = await db.run_sync(migration_module().inspect)
    return {f"{row['kind']}:{row['key']}": row["status"] for row in report}


async def _credential(engine, name: str, provider: str, datasets: list[str]) -> None:
    async with engine.begin() as db:
        await db.execute(
            text(
                "INSERT INTO source_client (client_id, name, owner, description, source_name, "
                "key_hash, fingerprint, allowed_datasets, rate_limit_requests, "
                "rate_limit_window, created_at, updated_at) VALUES (gen_random_uuid(), :name, "
                "'deployment', 'test', :provider, :hash, :fingerprint, "
                "CAST(:datasets AS jsonb), 2400, 60, now(), now())"
            ),
            {
                "name": name,
                "provider": provider,
                "hash": hashlib.sha256(name.encode()).hexdigest(),
                "fingerprint": hashlib.sha256(name.encode()).hexdigest()[:16],
                "datasets": json.dumps(datasets),
            },
        )


async def _scopes(engine) -> dict[str, list[str]]:
    async with engine.connect() as db:
        rows = await db.execute(text("SELECT name, allowed_datasets FROM source_client"))
        return {name: datasets for name, datasets in rows.all()}


@pytest.mark.asyncio
async def test_the_feeds_join_their_controls_with_the_config_seed_declares():
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "twse_tpex_institutional"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        # The deploy preflight runs the new inspection before the upgrade.
        assert all(status == "already_compliant" for status in (await _report(engine)).values())
        await _credential(engine, "fetcher-twse", "twse", ["tw_twse_index_eod"])
        # An operator's own scope is kept as it is.
        await _credential(engine, "fetcher-tpex", "tpex", ["tw_tpex_index_eod", "other"])

        await run_alembic(url, REVISION)

        async with engine.connect() as db:
            rows = {
                row["dataset_key"]: row
                for row in (
                    await db.execute(
                        text(
                            "SELECT dataset_key, name, description, asset_class, market, "
                            "frequency, is_active, config FROM dataset_registry "
                            "WHERE dataset_key LIKE 'tw_%_institutional_%'"
                        )
                    )
                ).mappings()
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
            clocks = dict(
                (
                    await db.execute(
                        text(
                            "SELECT scheduler_key, scheduled_local_time::text FROM scheduler_control "
                            "WHERE provider IN ('twse', 'tpex')"
                        )
                    )
                ).all()
            )
        assert sorted(rows) == DATASETS_ADDED
        # Exactly what seed declares, so registry provisioning has nothing to repair.
        for key in DATASETS_ADDED:
            definition = next(item for item in DATASETS if item["dataset_key"] == key)
            row = rows[key]
            fields = ("name", "description", "asset_class", "market", "frequency")
            assert {field: row[field] for field in fields} == {
                field: definition[field] for field in fields
            }
            assert row["is_active"] is True and row["config"] == definition["config"]
        assert scopes == SCOPES
        # Each control still shows its earliest feed, the index.
        assert clocks == dict.fromkeys(SCOPES, "14:15:00")
        assert await _scopes(engine) == {
            "fetcher-twse": sorted(SCOPES["full_market_twse_v1"]),
            "fetcher-tpex": ["tw_tpex_index_eod", "other"],
        }
        report = await _report(engine)
        assert all(report[f"dataset:{key}"] == "already_compliant" for key in DATASETS_ADDED)
        assert all(status == "already_compliant" for status in report.values())

        # A clock this revision did not install is an unknown difference for the deploy.
        tpex = next(
            item for item in DATASETS if item["dataset_key"] == "tw_tpex_institutional_stock"
        )
        changed = deepcopy(tpex["config"])
        changed["delivery_expectation"]["schedule"]["local_time"] = "15:00:00"
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE dataset_registry SET config = CAST(:config AS jsonb) "
                    "WHERE dataset_key = 'tw_tpex_institutional_stock'"
                ),
                {"config": json.dumps(changed)},
            )
        report = await _report(engine)
        assert report["dataset:tw_tpex_institutional_stock"] == "unknown_difference"
        assert report["dataset:tw_tpex_institutional_summary"] == "already_compliant"
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE dataset_registry SET config = CAST(:config AS jsonb) "
                    "WHERE dataset_key = 'tw_tpex_institutional_stock'"
                ),
                {"config": json.dumps(tpex["config"])},
            )

        # Once a dataset has been used, a schema rollback would orphan its history.
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO ingestion_run (run_id, dataset_key, source, status, raw_records, "
                    "total_records, success_records, failed_records, created_at) "
                    "VALUES (gen_random_uuid(), 'tw_twse_institutional_summary', 'twse', "
                    "'completed', 0, 0, 0, 0, now())"
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
                    "SELECT (SELECT count(*) FROM dataset_registry WHERE dataset_key LIKE "
                    "'tw_%_institutional_%') + (SELECT count(*) FROM scheduler_dataset "
                    "WHERE dataset_key LIKE 'tw_%_institutional_%')"
                )
            )
        assert remaining == 0
        # The index scope returns only where this revision widened it.
        assert await _scopes(engine) == {
            "fetcher-twse": ["tw_twse_index_eod"],
            "fetcher-tpex": ["tw_tpex_index_eod", "other"],
        }
