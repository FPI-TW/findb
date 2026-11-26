"""Mapping gaps move from blocked to unavailable counts, and fold back on downgrade (#593)."""

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "1587769858c0"
UNAVAILABLE = "c7e9a1b3d5f8"


async def _seed_plan(db, ids: dict, *, plan: str, part: str, synced: bool) -> None:
    """A plan of two members: one has data, the other is a frozen mapping gap."""
    await db.execute(
        text(
            "INSERT INTO daily_delivery_plan(plan_id,dataset_key,provider,trade_date,release_id,activation_cutoff,deadline_at,member_sha256,expected_count,execution_profile,created_at) VALUES(:plan,'tw_equity_eod','finlab',:day,:release,'2026-10-13',now(),repeat('b',64),2,'full_market',now())"
        ),
        {"plan": ids[plan], "release": ids["release"], "day": ids[f"{plan}_day"]},
    )
    counts = "1,0,0,1,1" if synced else "NULL,NULL,NULL,NULL,NULL"
    await db.execute(
        text(
            "INSERT INTO daily_delivery_part(part_id,plan_id,part_number,work_item_id,member_sha256,expected_count,data_count,no_data_count,missing_count,blocked_count,mapping_gap_count) "
            f"VALUES(:part,:plan,1,:work,repeat('c',64),2,{counts})"
        ),
        {"part": ids[part], "plan": ids[plan], "work": f"fp1:{part}"},
    )
    for key, status in (("2330", "mapped"), ("9999", "gap")):
        await db.execute(
            text(
                "INSERT INTO daily_delivery_member(member_id,plan_id,part_id,member_key,symbol,provider_symbol,mapping_status,outcome_history) VALUES(:id,:plan,:part,:key,:key,:key,:status,'[]')"
            ),
            {"id": uuid7(), "plan": ids[plan], "part": ids[part], "key": key, "status": status},
        )
    stamp = "now()" if synced else "NULL"
    await db.execute(
        text(
            "INSERT INTO daily_delivery_plan_summary(plan_id,expected_count,data_count,no_data_count,missing_count,blocked_count,updated_at,parts_synced_at) "
            f"VALUES(:plan,2,1,0,0,1,now(),{stamp})"
        ),
        {"plan": ids[plan]},
    )


async def _counts(db, table: str, key: str, value) -> tuple:
    row = await db.execute(
        text(f"SELECT blocked_count, unavailable_count FROM {table} WHERE {key} = :value"),
        {"value": value},
    )
    return tuple(row.one())


@pytest.mark.asyncio
async def test_unavailable_counts_take_the_mapping_gaps_and_fold_back_on_downgrade():
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "findb_unavailable_counts"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        ids = {
            "release": uuid7(),
            "synced": uuid7(),
            "synced_day": date(2026, 10, 13),
            "synced_part": uuid7(),
            "stale": uuid7(),
            "stale_day": date(2026, 10, 14),
            "stale_part": uuid7(),
        }
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
            await _seed_plan(db, ids, plan="synced", part="synced_part", synced=True)
            await _seed_plan(db, ids, plan="stale", part="stale_part", synced=False)
            await db.execute(
                text(
                    "UPDATE daily_delivery_plan_summary SET parts_synced_at = updated_at "
                    "WHERE plan_id = :plan"
                ),
                {"plan": ids["synced"]},
            )

        await run_alembic(url, UNAVAILABLE)

        async with engine.begin() as db:
            summary, part = "daily_delivery_plan_summary", "daily_delivery_part"
            # A summary written with its part counts moves its mapping gaps.
            assert await _counts(db, summary, "plan_id", ids["synced"]) == (0, 1)
            assert await _counts(db, part, "part_id", ids["synced_part"]) == (0, 1)
            # One that cannot be trusted keeps valid counts and takes a full refresh.
            assert await _counts(db, summary, "plan_id", ids["stale"]) == (1, 0)
            assert await _counts(db, part, "part_id", ids["stale_part"]) == (None, None)
            assert (
                await db.scalar(
                    text(f"SELECT parts_synced_at FROM {summary} WHERE plan_id = :plan"),
                    {"plan": ids["stale"]},
                )
                is None
            )
            for table, invalid in (
                (summary, "unavailable_count = 2"),  # the counts still add up to expected
                (part, "unavailable_count = 0"),  # mapping gaps are unavailable members
                (part, "unavailable_count = NULL"),  # counts are all present or all absent
            ):
                with pytest.raises(IntegrityError, match="summary_counts"):
                    async with db.begin_nested():
                        await db.execute(
                            text(f"UPDATE {table} SET {invalid} WHERE plan_id = :plan"),
                            {"plan": ids["synced"]},
                        )

        await run_alembic(url, BEFORE, command="downgrade")

        async with engine.connect() as db:
            for table, key, value in (
                ("daily_delivery_plan_summary", "plan_id", ids["synced"]),
                ("daily_delivery_part", "part_id", ids["synced_part"]),
            ):
                assert (
                    await db.scalar(
                        text(f"SELECT blocked_count FROM {table} WHERE {key} = :value"),
                        {"value": value},
                    )
                    == 1
                )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE column_name = 'unavailable_count'"
                    )
                )
                == 0
            )
