"""Part summary counts are additive, start untrusted and drop without touching plans."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "e8b3d5f7a9c2"
PART_COUNTS = "b4d6f8a1c3e5"


async def _part_columns(db) -> set[str]:
    rows = await db.execute(
        text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND ((table_name = 'daily_delivery_part' AND column_name LIKE '%_count' "
            "AND column_name <> 'expected_count') "
            "OR (table_name = 'daily_delivery_plan_summary' AND column_name = 'parts_synced_at'))"
        )
    )
    return set(rows.scalars())


@pytest.mark.asyncio
async def test_part_counts_roundtrip_keeps_plans_and_validates_counts():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "findb_part_counts"
    ) as (url, engine):
        await run_alembic(url, BEFORE, forward_columns=False)
        ids = {"release": uuid7(), "plan": uuid7(), "part": uuid7()}
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO dataset_registry(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at) VALUES ('tw_equity_eod','TW','equity','TW','daily',true,'{}',now(),now()) ON CONFLICT DO NOTHING"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO universe_release(release_id,dataset_key,provider,effective_date,observed_at,source_timezone,evidence,member_sha256,semantic_member_sha256,member_count,change_count,change_ratio,status,created_at) VALUES(:release,'tw_equity_eod','finlab','2026-10-13',now(),'Asia/Taipei','[]',repeat('a',64),repeat('a',64),2,2,1,'published',now())"
                ),
                ids,
            )
            await db.execute(
                text(
                    "INSERT INTO daily_delivery_plan(plan_id,dataset_key,provider,trade_date,release_id,activation_cutoff,deadline_at,member_sha256,expected_count,execution_profile,created_at) VALUES(:plan,'tw_equity_eod','finlab','2026-10-13',:release,'2026-10-13',now(),repeat('b',64),2,'full_market',now())"
                ),
                ids,
            )
            await db.execute(
                text(
                    "INSERT INTO daily_delivery_part(part_id,plan_id,part_number,work_item_id,member_sha256,expected_count) VALUES(:part,:plan,1,'fp1:part',repeat('c',64),2)"
                ),
                ids,
            )
            await db.execute(
                text(
                    "INSERT INTO daily_delivery_plan_summary(plan_id,expected_count,data_count,no_data_count,missing_count,blocked_count,updated_at) VALUES(:plan,2,0,0,2,0,now())"
                ),
                ids,
            )
        await run_alembic(url, PART_COUNTS)
        async with engine.begin() as db:
            assert len(await _part_columns(db)) == 6
            # Existing plans start untrusted, so their next write refreshes in full.
            assert (
                await db.scalar(
                    text("SELECT data_count FROM daily_delivery_part WHERE part_id=:part"), ids
                )
                is None
            )
            assert (
                await db.scalar(text("SELECT parts_synced_at FROM daily_delivery_plan_summary"))
                is None
            )
            await db.execute(
                text(
                    "UPDATE daily_delivery_part SET data_count=1, no_data_count=0, missing_count=0, blocked_count=1, mapping_gap_count=1"
                )
            )
            for invalid in (
                "data_count=2",  # parts must still add up to the frozen expected count
                "mapping_gap_count=2",  # mapping gaps are a subset of blocked members
                "missing_count=NULL",  # counts are all present or all absent
            ):
                with pytest.raises(IntegrityError, match="delivery_part_summary_counts"):
                    async with db.begin_nested():
                        await db.execute(text(f"UPDATE daily_delivery_part SET {invalid}"))
        await run_alembic(url, BEFORE, command="downgrade", forward_columns=False)
        async with engine.connect() as db:
            assert await _part_columns(db) == set()
            assert (
                await db.scalar(
                    text("SELECT expected_count FROM daily_delivery_part WHERE part_id=:part"), ids
                )
                == 2
            )
            assert (
                await db.scalar(text("SELECT missing_count FROM daily_delivery_plan_summary")) == 2
            )
        await run_alembic(url, PART_COUNTS)
        async with engine.connect() as db:
            assert len(await _part_columns(db)) == 6
