"""Unified cutover preserves full-market state and sets an explicit rollback floor."""

import subprocess

import pytest
from sqlalchemy import text

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory, run_alembic


@pytest.mark.asyncio
async def test_profile_upgrade_preserves_full_dates_and_frozen_plans():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "findb_profiles") as (
        url,
        engine,
    ):
        async with engine.begin() as db:
            await db.execute(
                text("""INSERT INTO dataset_registry(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at)
            VALUES ('tw_equity_eod','TW','equity','TW','daily',true,'{}',now(),now()) ON CONFLICT DO NOTHING""")
            )
            await db.execute(
                text(
                    "INSERT INTO full_market_dataset_state(dataset_key,first_start_date) VALUES ('tw_equity_eod','2026-10-13')"
                )
            )
            ids = {name: uuid7() for name in ("release", "plan", "part", "member")}
            statements = [
                """INSERT INTO universe_release(release_id,dataset_key,provider,effective_date,observed_at,
                source_timezone,evidence,member_sha256,member_count,change_count,change_ratio,status,created_at)
                VALUES(:release,'tw_equity_eod','finlab','2026-10-13',now(),'Asia/Taipei','[]',repeat('a',64),1,1,1,'published',now())""",
                """INSERT INTO daily_delivery_plan(plan_id,dataset_key,provider,trade_date,release_id,
                activation_cutoff,deadline_at,member_sha256,expected_count,created_at)
                VALUES(:plan,'tw_equity_eod','finlab','2026-10-13',:release,'2026-10-13',now(),repeat('b',64),1,now())""",
                """INSERT INTO daily_delivery_part(part_id,plan_id,part_number,work_item_id,member_sha256,expected_count)
                VALUES(:part,:plan,1,'fp1:frozen-before-upgrade',repeat('b',64),1)""",
                """INSERT INTO daily_delivery_member(member_id,plan_id,part_id,member_key,symbol,provider_symbol,
                mapping_status,outcome,outcome_reason,outcome_evidence,outcome_history,outcome_at)
                VALUES(:member,:plan,:part,'2330','2330','2330','mapped','blocked','source_error',
                '{"sha256":"preserve-evidence"}','[{"reason":"source_error"}]',now())""",
                """INSERT INTO scheduler_control(scheduler_key,provider,slot_id,scheduled_local_time,timezone,
                desired_state,observed_state,revision,created_at,updated_at)
                VALUES('legacy_fixture','finlab','taiwan_market_window','18:00','Asia/Taipei','running','running',7,now(),now())""",
            ]
            for statement in statements:
                await db.execute(text(statement), ids)
            frozen_before = {}
            for table in ("daily_delivery_plan", "daily_delivery_part", "daily_delivery_member"):
                frozen_before[table] = (
                    (await db.execute(text(f"SELECT to_jsonb(t) FROM {table} t"))).scalars().all()
                )
        try:
            await run_alembic(url, "91a4c7e2d630")
        except subprocess.CalledProcessError as failure:
            pytest.fail(failure.stderr)
        async with engine.begin() as db:
            assert (
                await db.execute(
                    text(
                        "SELECT dataset_key,execution_profile,first_start_date::text FROM full_market_dataset_state"
                    )
                )
            ).all() == [("tw_equity_eod", "full_market", "2026-10-13")]
            for table, before in frozen_before.items():
                after = (
                    (
                        await db.execute(
                            text(f"SELECT to_jsonb(t) - 'execution_profile' FROM {table} t")
                        )
                    )
                    .scalars()
                    .all()
                )
                assert after == before
            decision = await db.scalar(
                text("SELECT snapshot FROM daily_execution_decision WHERE plan_id=:plan"), ids
            )
            assert decision["snapshot_origin"] == "legacy_migration_snapshot"
            assert decision["calendar_revision"] is None
            assert decision["regular_close"] == "13:30:00"
            assert (
                await db.scalar(text("SELECT execution_profile FROM daily_delivery_plan"))
                == "full_market"
            )
            assert (
                await db.execute(
                    text(
                        "SELECT desired_state,revision,retired_at IS NOT NULL FROM scheduler_control WHERE scheduler_key='legacy_fixture'"
                    )
                )
            ).one() == ("stopped", 8, True)
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM scheduler_control WHERE retired_at IS NULL")
                )
                == 8
            )
            await db.execute(
                text(
                    "INSERT INTO full_market_dataset_state(dataset_key,execution_profile,first_start_date) VALUES ('tw_equity_eod','pilot','2026-10-14')"
                )
            )
            assert await db.scalar(text("SELECT count(*) FROM full_market_dataset_state")) == 2
            assert (
                await db.scalar(text("SELECT to_regclass('delivery_member_evidence')"))
                == "delivery_member_evidence"
            )
        with pytest.raises(subprocess.CalledProcessError) as failed:
            await run_alembic(url, "3553ed95cfbf", command="downgrade")
        assert "new rollback floor" in failed.value.stderr
