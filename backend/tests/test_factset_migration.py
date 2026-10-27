"""Expand-only cohort storage and fail-closed rollback once v2 evidence exists."""

import subprocess

import pytest
from sqlalchemy import text

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory, run_alembic


@pytest.mark.asyncio
async def test_eod_cohort_expansion_preserves_legacy_and_refuses_evidence_loss():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "eod_cohort") as (
        url,
        engine,
    ):
        await run_alembic(url, "c64eb01f8a32")
        ids = {"request": uuid7(), "item": uuid7(), "token": uuid7()}
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO dataset_registry(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at) "
                    "VALUES('hk_equity_eod','HK','equity','HK','daily',true,'{}',now(),now()) ON CONFLICT DO NOTHING"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO historical_backfill_request(request_id,request_key,provider,dataset_key,market,start_date,end_date,created_by,status,created_at,updated_at) "
                    "VALUES(:request,'legacy-scope','twelve_data','hk_equity_eod','HK','2026-10-15','2026-10-15','owner','running',now(),now())"
                ),
                ids,
            )
            await db.execute(
                text(
                    "INSERT INTO historical_backfill_item(item_id,request_id,trade_date,status,lease_attempt,lease_token,lease_expires_at) "
                    "VALUES(:item,:request,'2026-10-15','running',2,:token,now()+interval '1 hour')"
                ),
                ids,
            )
            before = await db.scalar(
                text("SELECT to_jsonb(i) FROM historical_backfill_item i WHERE item_id=:item"), ids
            )
        await run_alembic(url, "head")
        async with engine.begin() as db:
            assert (
                await db.scalar(
                    text("SELECT to_jsonb(i) FROM historical_backfill_item i WHERE item_id=:item"),
                    ids,
                )
                == before
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT universe_snapshot FROM historical_backfill_request WHERE request_id=:request"
                    ),
                    ids,
                )
                is None
            )
        await run_alembic(url, "c64eb01f8a32", command="downgrade")
        await run_alembic(url, "head")
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE historical_backfill_request SET status='partial', universe_snapshot='{\"cohort\":\"current_hkex\"}' WHERE request_id=:request"
                ),
                ids,
            )
            await db.execute(
                text("UPDATE historical_backfill_item SET status='partial' WHERE item_id=:item"),
                ids,
            )
        with pytest.raises(subprocess.CalledProcessError) as error:
            await run_alembic(url, "c64eb01f8a32", command="downgrade")
        assert "preserve cohort and partial evidence" in error.value.stderr
        async with engine.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version")) == "e86c1f4b2a90"
            )
            assert (
                await db.scalar(
                    text("SELECT status FROM historical_backfill_item WHERE item_id=:item"), ids
                )
                == "partial"
            )
