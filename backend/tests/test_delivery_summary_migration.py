"""An additive summary migration preserves frozen plans across upgrade/downgrade."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory, run_alembic


@pytest.mark.asyncio
async def test_summary_schema_roundtrip_preserves_plan_and_requires_valid_counts():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "findb_plan_summary"
    ) as (
        url,
        engine,
    ):
        await run_alembic(url, "f97d2a6c4b81")
        ids = {"release": uuid7(), "plan": uuid7()}
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO dataset_registry(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at) VALUES ('tw_equity_eod','TW','equity','TW','daily',true,'{}',now(),now()) ON CONFLICT DO NOTHING"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO universe_release(release_id,dataset_key,provider,effective_date,observed_at,source_timezone,evidence,member_sha256,semantic_member_sha256,member_count,change_count,change_ratio,status,created_at) VALUES(:release,'tw_equity_eod','finlab','2026-10-13',now(),'Asia/Taipei','[]',repeat('a',64),repeat('a',64),1,1,1,'published',now())"
                ),
                ids,
            )
            await db.execute(
                text(
                    "INSERT INTO daily_delivery_plan(plan_id,dataset_key,provider,trade_date,release_id,activation_cutoff,deadline_at,member_sha256,expected_count,execution_profile,created_at) VALUES(:plan,'tw_equity_eod','finlab','2026-10-13',:release,'2026-10-13',now(),repeat('b',64),1,'full_market',now())"
                ),
                ids,
            )
        await run_alembic(url, "head")
        async with engine.begin() as db:
            assert await db.scalar(text("SELECT count(*) FROM daily_delivery_plan_summary")) == 0
            assert (
                await db.scalar(
                    text("SELECT expected_count FROM daily_delivery_plan WHERE plan_id=:plan"), ids
                )
                == 1
            )
            await db.execute(
                text(
                    "INSERT INTO daily_delivery_plan_summary(plan_id,expected_count,data_count,no_data_count,missing_count,blocked_count,updated_at) VALUES(:plan,1,0,0,1,0,now())"
                ),
                ids,
            )
            with pytest.raises(IntegrityError, match="delivery_plan_summary_counts"):
                async with db.begin_nested():
                    await db.execute(text("UPDATE daily_delivery_plan_summary SET data_count=2"))
        await run_alembic(url, "f97d2a6c4b81", command="downgrade")
        async with engine.connect() as db:
            assert (
                await db.scalar(text("SELECT to_regclass('daily_delivery_plan_summary')")) is None
            )
            assert (
                await db.scalar(
                    text("SELECT expected_count FROM daily_delivery_plan WHERE plan_id=:plan"), ids
                )
                == 1
            )
        await run_alembic(url, "head")
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM daily_delivery_plan_summary")) == 0
