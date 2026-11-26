"""Staging the FinLab intraday feeds adds datasets, scheduler scope and credential scope together."""

import json
import subprocess
from copy import deepcopy

import pytest
from sqlalchemy import text

from app.services.source_clients import PROVIDER_DATASET_SCOPE
from app.utils import uuid7
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "e7a9c1d3f5b8"
STAGED = "f8b0d2c4e6a9"
ACTIVATED = "a9c1e3f5b7d0"
INTRADAY = ("tw_equity_bar_1m", "tw_etf_bar_1m", "tw_equity_tick", "tw_etf_tick")
CLIENT = (
    "INSERT INTO source_client(client_id,name,source_name,key_hash,allowed_datasets,"
    "rate_limit_requests,rate_limit_window,usage_count,created_at,updated_at,revoked_at) "
    "VALUES(:id,:name,:source,:hash,CAST(:scope AS jsonb),100,60,0,now(),now(),"
    "CASE WHEN :revoked THEN now() END)"
)


async def _scopes(db) -> dict[str, list[str] | None]:
    rows = await db.execute(text("SELECT name, allowed_datasets FROM source_client"))
    return dict(rows.all())


@pytest.mark.asyncio
async def test_intraday_feeds_are_staged_inactive_with_the_scopes_the_catalog_now_requires():
    async with get_active_migration_database_factory().clone(
        "e9f4a1c6b3d5", "findb_intraday_feeds"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        clients = {
            "fetcher": ("finlab", ["tw_equity_eod", "tw_etf_eod"], False),
            "reordered": (" FinLab ", ["tw_etf_eod", "tw_equity_eod"], False),
            "narrow": ("finlab", ["tw_equity_eod"], False),
            "revoked": ("finlab", ["tw_equity_eod", "tw_etf_eod"], True),
            "other": ("twelve_data", ["us_equity_eod"], False),
            "unscoped": ("finlab", None, False),
        }
        async with engine.begin() as db:
            for index, (name, (source, scope, revoked)) in enumerate(clients.items()):
                await db.execute(
                    text(CLIENT),
                    {
                        "id": uuid7(),
                        "name": name,
                        "source": source,
                        "hash": f"{index:064d}",
                        "scope": json.dumps(scope),
                        "revoked": revoked,
                    },
                )
            before_datasets = set(
                (
                    await db.scalars(
                        text(
                            "SELECT dataset_key FROM scheduler_dataset "
                            "WHERE scheduler_key='full_market_finlab_v1'"
                        )
                    )
                ).all()
            )
        assert before_datasets == {"tw_equity_eod", "tw_etf_eod"}

        await run_alembic(url, STAGED)

        staged_scope = list(PROVIDER_DATASET_SCOPE["finlab"])
        async with engine.begin() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT dataset_key, name, asset_class, market, frequency, is_active, config "
                        "FROM dataset_registry WHERE dataset_key = ANY(:keys)"
                    ),
                    {"keys": list(INTRADAY)},
                )
            ).mappings()
            staged = {row["dataset_key"]: row for row in rows}
            # Exactly what seed declares, so provisioning finds nothing to repair.
            for definition in DATASETS:
                if definition["dataset_key"] in INTRADAY:
                    row = staged[definition["dataset_key"]]
                    assert row["is_active"] is False
                    assert {k: row[k] for k in ("name", "asset_class", "market", "frequency")} == {
                        k: definition[k] for k in ("name", "asset_class", "market", "frequency")
                    }
                    # Seed's config, at the start this revision staged (d7f9b1c3e5a8 moves it).
                    expected = deepcopy(definition["config"])
                    expected["delivery_expectation"]["schedule"]["local_time"] = "18:30:00"
                    assert row["config"] == expected
            assert set(staged) == set(INTRADAY)
            assert set(
                (
                    await db.scalars(
                        text(
                            "SELECT dataset_key FROM scheduler_dataset "
                            "WHERE scheduler_key='full_market_finlab_v1'"
                        )
                    )
                ).all()
            ) == set(staged_scope)
            # Only a live credential holding FinLab's whole previous scope is widened.
            assert await _scopes(db) == {
                "fetcher": staged_scope,
                "reordered": staged_scope,
                "narrow": ["tw_equity_eod"],
                "revoked": ["tw_equity_eod", "tw_etf_eod"],
                "other": ["us_equity_eod"],
                "unscoped": None,
            }
            # Once a dataset has been used the rollback would orphan its history.
            await db.execute(
                text(
                    "INSERT INTO intraday_partition(dataset_key,symbol,trade_date,row_count,source,"
                    "source_raw_sha256,source_rows_sha256,source_fetched_at,object_layout,object_key,"
                    "object_etag,created_at,updated_at) VALUES('tw_equity_tick','2330','2026-11-13',"
                    "1,'finlab',repeat('a',64),repeat('b',64),now(),'day','k','e',now(),now())"
                )
            )

        with pytest.raises(subprocess.CalledProcessError) as blocked:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "downgrade is blocked" in blocked.value.stderr
        async with engine.begin() as db:
            await db.execute(text("DELETE FROM intraday_partition"))

        await run_alembic(url, BEFORE, command="downgrade")
        async with engine.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM dataset_registry WHERE dataset_key = ANY(:keys)"),
                    {"keys": list(INTRADAY)},
                )
                == 0
            )
            scopes = await _scopes(db)
            assert scopes["fetcher"] == ["tw_equity_eod", "tw_etf_eod"]
            assert scopes["narrow"] == ["tw_equity_eod"] and scopes["unscoped"] is None
        # Staging it again is repeatable.
        await run_alembic(url, STAGED)
        async with engine.connect() as db:
            assert (await _scopes(db))["fetcher"] == staged_scope


async def _active(db) -> dict[str, bool]:
    rows = await db.execute(
        text("SELECT dataset_key, is_active FROM dataset_registry WHERE dataset_key = ANY(:keys)"),
        {"keys": list(INTRADAY)},
    )
    return dict(rows.all())


@pytest.mark.asyncio
async def test_the_intraday_datasets_are_active_like_every_governed_dataset():
    async with get_active_migration_database_factory().clone(
        "e9f4a1c6b3d5", "findb_intraday_active"
    ) as (url, engine):
        await run_alembic(url, STAGED)
        async with engine.connect() as db:
            assert await _active(db) == dict.fromkeys(INTRADAY, False)
            others = await db.scalar(text("SELECT count(*) FROM dataset_registry WHERE is_active"))

        await run_alembic(url, ACTIVATED)

        async with engine.connect() as db:
            assert await _active(db) == dict.fromkeys(INTRADAY, True)
            # Seed declares them active too, so a later seed run agrees with the migration.
            assert all(
                definition["is_active"]
                for definition in DATASETS
                if definition["dataset_key"] in INTRADAY
            )
            # No other dataset is touched.
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM dataset_registry "
                        "WHERE is_active AND dataset_key <> ALL(:keys)"
                    ),
                    {"keys": list(INTRADAY)},
                )
                == others
            )

        await run_alembic(url, STAGED, command="downgrade")
        async with engine.connect() as db:
            assert await _active(db) == dict.fromkeys(INTRADAY, False)
