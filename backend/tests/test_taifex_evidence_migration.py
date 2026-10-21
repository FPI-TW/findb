"""TAIFEX historical evidence migration keeps existing backfill items."""

import json
import subprocess

import pytest
from sqlalchemy import text

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory, run_alembic
from tests.test_taifex_historical import evidence


@pytest.mark.asyncio
async def test_evidence_migration_is_additive_and_keeps_existing_items():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "findb_taifex_evidence"
    ) as (url, engine):
        await run_alembic(url, "91a4c7e2d630")
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO dataset_registry(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at) VALUES ('tw_futures_eod','TAIFEX','future','TW','daily',true,'{}',now(),now()) ON CONFLICT DO NOTHING"
                )
            )
            ids = {"request": uuid7(), "item": uuid7()}
            await db.execute(
                text(
                    "INSERT INTO historical_backfill_request(request_id,request_key,provider,dataset_key,market,start_date,end_date,created_by,status,created_at,updated_at) VALUES (:request,'migration-evidence','taifex','tw_futures_eod','TW','2026-10-01','2026-10-01','operator','queued',now(),now())"
                ),
                ids,
            )
            await db.execute(
                text(
                    "INSERT INTO historical_backfill_item(item_id,request_id,trade_date,status,lease_attempt,calendar_snapshot) VALUES (:item,:request,'2026-10-01','pending',0,'{\"market\":\"TAIFEX\"}')"
                ),
                ids,
            )
            before = await db.scalar(text("SELECT count(*) FROM historical_backfill_item"))
            before_item = await db.scalar(
                text("SELECT to_jsonb(t) FROM historical_backfill_item t WHERE item_id=:item"), ids
            )
        await run_alembic(url, "a42c89d3e610")
        async with engine.begin() as db:
            assert await db.scalar(text("SELECT count(*) FROM historical_backfill_item")) == before
            after_item = await db.scalar(
                text("SELECT to_jsonb(t) FROM historical_backfill_item t WHERE item_id=:item"), ids
            )
            assert after_item.pop("result_evidence") is None
            assert after_item == before_item
            column = (
                await db.execute(
                    text(
                        "SELECT data_type,is_nullable FROM information_schema.columns WHERE table_name='historical_backfill_item' AND column_name='result_evidence'"
                    )
                )
            ).one()
            assert column == ("jsonb", "YES")
            proof = evidence(ids["item"], ids["request"], no_data=True)
            await db.execute(
                text(
                    "UPDATE historical_backfill_item SET result_evidence=CAST(:evidence AS jsonb) WHERE item_id=:item"
                ),
                {"item": ids["item"], "evidence": json.dumps(proof)},
            )
        with pytest.raises(subprocess.CalledProcessError) as failed:
            await run_alembic(url, "91a4c7e2d630", command="downgrade")
        assert "Historical evidence must be retained; use a forward fix" in failed.value.stderr
        async with engine.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version")) == "a42c89d3e610"
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT result_evidence FROM historical_backfill_item WHERE item_id=:item"
                    ),
                    ids,
                )
                == proof
            )
