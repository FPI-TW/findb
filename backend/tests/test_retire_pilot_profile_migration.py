"""Pilot retirement refuses unsettled work, deletes Pilot workflow rows and keeps data."""

import subprocess
from copy import deepcopy
from datetime import date, datetime, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_work import (
    ProviderWork,
    ProviderWorkAttempt,
    ProviderWorkPrepared,
    ProviderWorkReceipt,
)
from app.models.registry import (
    DatasetRegistry,
    DeliveryMemberEvidence,
    IngestionRun,
    MissingDeliveryAlert,
    SchedulerControl,
)
from app.utils import uuid7
from scripts.seed_data import DATASETS
from tests.migration_database import get_active_migration_database_factory, run_alembic
from tests.test_full_market import _activated_plan

BEFORE = "e9f4a1c6b3d5"
HEAD = "a7c3e1f9d2b4"
NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
PROFILE_TABLES = (
    "scheduler_control",
    "full_market_dataset_state",
    "daily_delivery_plan",
    "daily_execution_decision",
    "missing_delivery_alert",
    "provider_work",
)


async def _clone(db, table: str, where: str, params: dict, overrides: dict) -> None:
    """Copy rows as Pilot rows using the live column set of ``table``."""
    pairs = ", ".join(f"'{column}', {value}" for column, value in overrides.items())
    await db.execute(
        text(
            f"INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table}, "
            f"to_jsonb(t) || jsonb_build_object({pairs}))).* FROM {table} t WHERE {where}"
        ),
        params,
    )


async def _seed(db) -> dict:
    async def existing_dataset(session, key):
        dataset = await session.get(DatasetRegistry, key)
        dataset.config = deepcopy(next(d["config"] for d in DATASETS if d["dataset_key"] == key))
        await session.commit()
        return dataset

    with patch("tests.test_full_market._dataset", existing_dataset):
        dataset, _, full = await _activated_plan(db, "tw_futures_eod", count=1)
    pilot_plan, pilot_part, pilot_admission = uuid4(), uuid4(), uuid4()
    params = {"plan": full.plan_id, "pilot_plan": pilot_plan, "pilot_part": pilot_part}
    await _clone(
        db,
        "daily_delivery_plan",
        "plan_id = :plan",
        params,
        {"plan_id": "CAST(:pilot_plan AS uuid)", "execution_profile": "'pilot'"},
    )
    assert len(full.parts) == 1
    await _clone(
        db,
        "daily_delivery_part",
        "plan_id = :plan",
        params,
        {
            "part_id": "CAST(:pilot_part AS uuid)",
            "plan_id": "CAST(:pilot_plan AS uuid)",
            "work_item_id": "gen_random_uuid()",
        },
    )
    await _clone(
        db,
        "daily_delivery_member",
        "plan_id = :plan",
        params,
        {
            "member_id": "gen_random_uuid()",
            "plan_id": "CAST(:pilot_plan AS uuid)",
            "part_id": "CAST(:pilot_part AS uuid)",
        },
    )
    await _clone(
        db,
        "daily_execution_decision",
        "plan_id = :plan",
        params,
        {"plan_id": "CAST(:pilot_plan AS uuid)", "execution_profile": "'pilot'"},
    )
    await _clone(
        db,
        "full_market_dataset_state",
        "dataset_key = 'tw_futures_eod' AND execution_profile = 'full_market'",
        params,
        {"execution_profile": "'pilot'"},
    )
    full_admission = await db.scalar(
        text(
            "SELECT admission_id FROM full_market_admission "
            "WHERE scheduler_key = 'full_market_taifex_v1' LIMIT 1"
        )
    )
    admission_params = {"admission": full_admission, "pilot_admission": pilot_admission}
    await _clone(
        db,
        "full_market_admission",
        "admission_id = :admission",
        admission_params,
        {"admission_id": "CAST(:pilot_admission AS uuid)", "scheduler_key": "'pilot_taifex_v1'"},
    )
    await _clone(
        db,
        "full_market_admission_feed",
        "admission_id = :admission",
        admission_params,
        {"admission_id": "CAST(:pilot_admission AS uuid)"},
    )
    member_key = await db.scalar(
        text("SELECT member_key FROM daily_delivery_member WHERE plan_id = :p LIMIT 1"),
        {"p": pilot_plan},
    )
    work = ProviderWork(
        work_id=uuid7(),
        environment="local",
        provider="taifex",
        stable_key=f"daily:pilot:tw_futures_eod:{full.trade_date}",
        kind="daily",
        dataset_key="tw_futures_eod",
        trade_date=full.trade_date,
        execution_profile="pilot",
        manifest={"plan_id": str(pilot_plan)},
        state="completed",
    )
    db.add(work)
    await db.flush()
    attempt = ProviderWorkAttempt(work_id=work.work_id, attempt_number=1, state="settled")
    run = IngestionRun(
        dataset_key="tw_futures_eod",
        source="taifex",
        schema_id="futures_eod",
        schema_version=1,
        status="completed",
        delivery_part_id=pilot_part,
    )
    db.add_all([attempt, run])
    await db.flush()
    db.add_all(
        [
            ProviderWorkPrepared(
                work_id=work.work_id,
                delivery_key=member_key,
                descriptor={"body_sha256": "a" * 64},
                body='{"pilot":true}',
            ),
            ProviderWorkReceipt(
                run_id=run.run_id,
                work_id=work.work_id,
                attempt_id=attempt.attempt_id,
                member_key=member_key,
                payload_sha256="a" * 64,
            ),
            DeliveryMemberEvidence(run_id=run.run_id, member_key=member_key),
        ]
    )

    def alert(day, profile, code):
        return MissingDeliveryAlert(
            alert_id=uuid7(),
            dataset_key="tw_futures_eod",
            source="taifex",
            schema_id="futures_eod",
            schema_version=1,
            execution_profile=profile,
            expected_data_date=day,
            status="open",
            first_detected_at=NOW,
            last_detected_at=NOW,
            details={"code": code},
        )

    pilot_alert = alert(date(2026, 10, 1), "pilot", "PILOT_INCOMPLETE")
    legacy_alert = alert(date(2026, 10, 2), "pilot", "DATASET_DELIVERY_MISSING")
    duplicate = alert(date(2026, 10, 3), "pilot", "DATASET_DELIVERY_MISSING")
    kept = alert(date(2026, 10, 3), "full_market", "FULL_MARKET_INCOMPLETE")
    db.add_all([pilot_alert, legacy_alert, duplicate, kept])
    db.add(
        SchedulerControl(
            scheduler_key="legacy_taifex_daily",
            provider="taifex",
            execution_profile="pilot",
            slot_id="taiwan_market_window",
            timezone="Asia/Taipei",
            retired_at=NOW,
        )
    )
    await db.commit()
    return {
        "full_plan": full.plan_id,
        "pilot_plan": pilot_plan,
        "run": run.run_id,
        "work": work.work_id,
        "legacy_alert": legacy_alert.alert_id,
        "duplicate_alert": duplicate.alert_id,
        "kept_alert": kept.alert_id,
    }


async def _pilot_counts(engine) -> dict[str, int]:
    async with engine.connect() as db:
        counts = {
            table: await db.scalar(
                text(f"SELECT count(*) FROM {table} WHERE execution_profile = 'pilot'")
            )
            for table in PROFILE_TABLES
        }
        counts["pilot_controls"] = await db.scalar(
            text("SELECT count(*) FROM scheduler_control WHERE scheduler_key LIKE 'pilot\\_%'")
        )
        return counts


@pytest.mark.asyncio
async def test_pilot_retirement_deletes_workflow_rows_and_keeps_ingested_data():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "retire_pilot") as (
        url,
        engine,
    ):
        await run_alembic(url, BEFORE)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            seeded = await _seed(db)
        assert all((await _pilot_counts(engine)).values())

        await run_alembic(url, HEAD)

        assert set((await _pilot_counts(engine)).values()) == {0}
        async with engine.connect() as db:
            for table, column in (
                ("daily_delivery_plan", "plan_id"),
                ("daily_delivery_part", "plan_id"),
                ("daily_delivery_member", "plan_id"),
            ):
                assert not await db.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE {column} = :p"),
                    {"p": seeded["pilot_plan"]},
                )
            assert await db.scalar(
                text("SELECT count(*) FROM daily_delivery_plan WHERE plan_id = :p"),
                {"p": seeded["full_plan"]},
            )
            for table in (
                "provider_work_attempt",
                "provider_work_prepared",
                "provider_work_receipt",
            ):
                assert not await db.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE work_id = :w"), {"w": seeded["work"]}
                )
            assert not await db.scalar(
                text(
                    "SELECT count(*) FROM full_market_admission "
                    "WHERE scheduler_key LIKE 'pilot\\_%'"
                )
            )
            # Ingested data stays; it is only detached from the deleted plan part.
            run = (
                await db.execute(
                    text("SELECT status, delivery_part_id FROM ingestion_run WHERE run_id = :r"),
                    {"r": seeded["run"]},
                )
            ).one()
            assert run == ("completed", None)
            assert not await db.scalar(
                text("SELECT count(*) FROM delivery_member_evidence WHERE run_id = :r"),
                {"r": seeded["run"]},
            )
            alerts = dict(
                (
                    await db.execute(
                        text("SELECT alert_id, execution_profile FROM missing_delivery_alert")
                    )
                ).all()
            )
            assert alerts == {
                seeded["legacy_alert"]: "full_market",
                seeded["kept_alert"]: "full_market",
            }
            assert (
                await db.scalar(
                    text(
                        "SELECT execution_profile FROM scheduler_control "
                        "WHERE scheduler_key = 'legacy_taifex_daily'"
                    )
                )
                == "full_market"
            )
            assert {
                row[0]
                for row in (
                    await db.execute(
                        text(
                            "SELECT column_default FROM information_schema.columns "
                            "WHERE column_name = 'execution_profile' "
                            "AND table_name IN ('scheduler_control','missing_delivery_alert')"
                        )
                    )
                ).all()
            } == {"'full_market'::character varying"}
        for statement in (
            "UPDATE daily_delivery_plan SET execution_profile = 'pilot'",
            "UPDATE scheduler_control SET execution_profile = 'pilot'",
            "UPDATE missing_delivery_alert SET execution_profile = 'pilot'",
        ):
            async with engine.begin() as db:
                with pytest.raises(DBAPIError, match="check constraint"):
                    await db.execute(text(statement))
        with pytest.raises(subprocess.CalledProcessError) as downgrade:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "cannot be downgraded" in downgrade.value.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["controls", "work", "runs"])
async def test_pilot_retirement_refuses_unsettled_pilot_state(blocker):
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", f"retire_pilot_{blocker}"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            seeded = await _seed(db)
        async with engine.begin() as db:
            if blocker == "controls":
                await db.execute(
                    text(
                        "UPDATE scheduler_control SET desired_state = 'running' "
                        "WHERE scheduler_key = 'pilot_taifex_v1'"
                    )
                )
            elif blocker == "work":
                await db.execute(
                    text("UPDATE provider_work SET state = 'queued' WHERE work_id = :w"),
                    {"w": seeded["work"]},
                )
            else:
                await db.execute(
                    text("UPDATE ingestion_run SET status = 'processing' WHERE run_id = :r"),
                    {"r": seeded["run"]},
                )
        before = await _pilot_counts(engine)
        with pytest.raises(subprocess.CalledProcessError) as upgrade:
            await run_alembic(url, HEAD)
        assert f"Pilot retirement blocked by unsettled {blocker}" in upgrade.value.stderr
        assert await _pilot_counts(engine) == before
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == BEFORE
            assert isinstance(
                await db.scalar(
                    text("SELECT plan_id FROM daily_delivery_plan WHERE plan_id = :p"),
                    {"p": seeded["pilot_plan"]},
                ),
                UUID,
            )
