"""Admission migration preserves safe cutoffs and keeps malformed legacy dates blocked."""

import json

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory
from tests.test_migration_upgrade import _run_alembic


@pytest.mark.asyncio
async def test_admission_migration_preserves_valid_dates_without_invalid_cast_failure():
    async with get_active_migration_database_factory().clone("1331cb73adad", "findb_admission") as (
        url,
        engine,
    ):
        async with engine.begin() as db:
            for key, value in [
                ("valid", "2026-09-30"),
                ("invalid", "2026-02-30"),
                ("malformed", "not-a-date"),
            ]:
                await db.execute(
                    text("""INSERT INTO dataset_registry(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at)
                VALUES(:key,:key,'equity','TW','daily',false,CAST(:config AS jsonb),now(),now())"""),
                    {
                        "key": key,
                        "config": json.dumps(
                            {"full_market": {"required": True, "activation_date": value}}
                        ),
                    },
                )
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE dataset_registry SET is_active=true WHERE dataset_key='malformed'")
            )
        await _run_alembic(url, "2442dc84beae")
        async with engine.connect() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT dataset_key,first_start_date FROM full_market_dataset_state WHERE dataset_key IN ('valid','invalid','malformed')"
                    )
                )
            ).all()
            assert [(key, str(day)) for key, day in rows] == [("valid", "2026-09-30")]
            assert (
                await db.execute(
                    text(
                        "SELECT bool_and(is_active) FROM dataset_registry WHERE dataset_key IN ('valid','invalid','malformed')"
                    )
                )
            ).scalar()
            for name in (
                "full_market_environment",
                "full_market_enrollment",
                "full_market_admission",
                "full_market_admission_feed",
                "full_market_dataset_state",
            ):
                assert (
                    await db.execute(text("SELECT to_regclass(:name)"), {"name": name})
                ).scalar() == name
        await _run_alembic(url, "1331cb73adad", command="downgrade")
        async with engine.connect() as db:
            policies = dict(
                (
                    await db.execute(
                        text(
                            "SELECT dataset_key,is_active FROM dataset_registry WHERE dataset_key IN ('valid','invalid','malformed')"
                        )
                    )
                ).all()
            )
            assert policies == {"valid": False, "invalid": False, "malformed": True}
            assert not (
                await db.execute(
                    text(
                        "SELECT config->'full_market' ? '_registry_active_before_2442dc84beae' FROM dataset_registry WHERE dataset_key='valid'"
                    )
                )
            ).scalar()
        await _run_alembic(url, "2442dc84beae")


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [0, False, [], {}, "invalid", "2026-02-30"])
async def test_migrated_malformed_metadata_blocks_seeded_eligibility_and_start(monkeypatch, legacy):
    from copy import deepcopy

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.registry import DatasetRegistry, FullMarketDatasetState
    from app.services.full_market_admission import eligibility
    from app.services.scheduler_control import (
        SchedulerControlStartBlockedError,
        update_scheduler_desired_state,
    )
    from scripts.seed_data import DATASETS
    from tests.test_scheduler_start import KEY, PRINCIPAL, ready

    async with get_active_migration_database_factory().clone(
        "1331cb73adad", "findb_admission_bad"
    ) as (url, engine):
        seed = deepcopy(next(row for row in DATASETS if row["dataset_key"] == "tw_equity_eod"))
        seed["config"]["full_market"]["activation_date"] = legacy
        async with engine.begin() as db:
            await db.execute(
                text("""INSERT INTO dataset_registry(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at)
                VALUES('tw_equity_eod','TW equity','equity','TW','daily',true,CAST(:config AS jsonb),now(),now())
                ON CONFLICT(dataset_key) DO UPDATE SET config=excluded.config"""),
                {"config": json.dumps(seed["config"])},
            )
        await _run_alembic(url, "2442dc84beae")
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await ready(db, monkeypatch, scope=["tw_equity_eod"])
            dataset = await db.get(DatasetRegistry, "tw_equity_eod")
            assert dataset.config["full_market"]["activation_date"] == legacy
            assert await db.get(FullMarketDatasetState, "tw_equity_eod") is None
            result = await eligibility(db, "finlab", [dataset])
            assert not result["feeds"][0]["ready"]
            with pytest.raises(SchedulerControlStartBlockedError):
                await update_scheduler_desired_state(
                    db,
                    scheduler_key=KEY,
                    desired_state="running",
                    expected_revision=1,
                    principal=PRINCIPAL,
                )
            assert dataset.config["full_market"]["activation_date"] == legacy
