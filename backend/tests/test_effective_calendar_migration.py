"""Calendar-policy upgrade preserves existing historical items without re-planning."""

import json

import pytest
from sqlalchemy import text

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory, run_alembic


@pytest.mark.asyncio
async def test_calendar_policy_upgrade_preserves_existing_historical_work():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "findb_effective_calendar"
    ) as (url, engine):
        await run_alembic(url, "6b93ef20a147")
        ids = {key: uuid7() for key in ("request", "item", "revision", "day")}
        async with engine.begin() as db:
            await db.execute(
                text("""INSERT INTO dataset_registry(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at)
                VALUES ('tw_equity_eod','TW','equity','TW','daily',true,'{}',now(),now()) ON CONFLICT DO NOTHING""")
            )
            await db.execute(
                text("""INSERT INTO historical_backfill_request(request_id,request_key,provider,dataset_key,market,start_date,end_date,status,created_by,created_at,updated_at)
                VALUES (:request,'migration-frozen','finlab','tw_equity_eod','TW','2026-10-10','2026-10-12','queued','owner',now(),now())"""),
                ids,
            )
            await db.execute(
                text("""INSERT INTO historical_backfill_item(item_id,request_id,trade_date,status,lease_attempt)
                VALUES (:item,:request,'2026-10-12','pending',3)"""),
                ids,
            )
            before_request = await db.scalar(
                text(
                    "SELECT to_jsonb(t) FROM historical_backfill_request t WHERE request_id=:request"
                ),
                ids,
            )
            before_item = await db.scalar(
                text("SELECT to_jsonb(t) FROM historical_backfill_item t WHERE item_id=:item"), ids
            )
        await run_alembic(url, "91a4c7e2d630")
        async with engine.begin() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT to_jsonb(t) FROM historical_backfill_request t WHERE request_id=:request"
                    ),
                    ids,
                )
                == before_request
            )
            after = await db.scalar(
                text("SELECT to_jsonb(t) FROM historical_backfill_item t WHERE item_id=:item"), ids
            )
            snapshot = after.pop("calendar_snapshot")
            assert after == before_item
            assert snapshot["calendar_source"] == "weekday_default"
            assert snapshot["snapshot_origin"] == "legacy_migration_snapshot"
            assert snapshot["policy"]["skip_non_trading_days"] is False
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM historical_backfill_item WHERE request_id=:request"),
                    ids,
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns WHERE table_name='full_market_admission_feed' AND column_name='calendar_revision_id'"
                    )
                )
                == "YES"
            )
            await db.execute(
                text(
                    "UPDATE historical_backfill_item SET status='skipped_calendar', calendar_snapshot=CAST(:snapshot AS jsonb) WHERE item_id=:item"
                ),
                {"item": ids["item"], "snapshot": json.dumps(snapshot)},
            )
