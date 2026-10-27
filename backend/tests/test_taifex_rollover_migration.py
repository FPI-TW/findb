"""TAIFEX semantic identity migration preserves existing immutable releases."""

import hashlib
import json

import pytest
from sqlalchemy import text

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory, run_alembic


@pytest.mark.asyncio
async def test_semantic_digest_backfill_excludes_raw_evidence_reference() -> None:
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "taifex_semantic") as (
        url,
        engine,
    ):
        await run_alembic(url, "d75fc12a9b43")
        release_id = uuid7()
        member_id = uuid7()
        member = {
            "member_key": "TX|TX:202611|regular",
            "symbol": "TX",
            "provider_symbol": "TX:202611",
            "exchange": "TAIFEX",
            "currency": "TWD",
            "asset_class": "future",
            "classification": "monthly",
            "contract_code": "TX:202611",
            "product_code": "TX",
            "contract_month": "202611",
            "session": "regular",
            "mapping_status": "mapped",
        }
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO dataset_registry"
                    "(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at) "
                    "VALUES('tw_futures_eod','TAIFEX','future','TW','daily',true,'{}',now(),now()) "
                    "ON CONFLICT DO NOTHING"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO universe_release"
                    "(release_id,dataset_key,provider,effective_date,observed_at,source_timezone,"
                    "evidence,member_sha256,member_count,change_count,change_ratio,status,created_at) "
                    "VALUES(:release_id,'tw_futures_eod','taifex','2026-10-22',now(),"
                    "'Asia/Taipei','[]',:member_sha256,1,2,2,'candidate',now())"
                ),
                {"release_id": release_id, "member_sha256": "a" * 64},
            )
            await db.execute(
                text(
                    "INSERT INTO universe_member"
                    "(member_id,release_id,member_key,symbol,provider_symbol,exchange,currency,"
                    "asset_class,classification,contract_code,product_code,contract_month,session,"
                    "mapping_status,raw_evidence_ref) "
                    "VALUES(:member_id,:release_id,:member_key,:symbol,:provider_symbol,:exchange,"
                    ":currency,:asset_class,:classification,:contract_code,:product_code,"
                    ":contract_month,:session,:mapping_status,'r2://daily-evidence')"
                ),
                {"member_id": member_id, "release_id": release_id, **member},
            )

        await run_alembic(url, "e86c1f4b2a90")
        async with engine.begin() as db:
            await db.execute(
                text(
                    "UPDATE universe_release SET semantic_member_sha256=:legacy_digest "
                    "WHERE release_id=:release_id"
                ),
                {"release_id": release_id, "legacy_digest": "b" * 64},
            )
        await run_alembic(url, "head")

        expected = hashlib.sha256(
            json.dumps([member], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        async with engine.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT semantic_member_sha256 FROM universe_release "
                        "WHERE release_id=:release_id"
                    ),
                    {"release_id": release_id},
                )
                == expected
            )
