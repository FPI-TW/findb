"""Twelve Data HK scope retirement keeps evidence, FactSet and any other HK source."""

import json
import subprocess
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "9c1e4f7a2b6d"
HEAD = "c7d2e9a4f1b3"
CONFIG = {
    "allowed_sources": ["twelve_data", "factset", "future_hk_source"],
    "source_precedence": ["twelve_data"],
}


async def _seed(db) -> dict:
    ids = {name: uuid7() for name in ("work", "active", "revoked", "factset", "open", "us", "done")}
    await db.execute(
        text(
            "INSERT INTO dataset_registry"
            "(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at)"
            " VALUES (:key,:key,'equity',:market,'daily',true,CAST(:config AS jsonb),now(),now())"
            " ON CONFLICT (dataset_key) DO UPDATE SET config = excluded.config"
        ),
        [
            {"key": "hk_equity_eod", "market": "HK", "config": json.dumps(CONFIG)},
            {"key": "us_equity_eod", "market": "US", "config": "{}"},
        ],
    )
    for provider, datasets in (
        ("twelve_data", ("us_equity_eod", "hk_equity_eod")),
        ("factset", ("hk_equity_eod",)),
    ):
        for profile in ("pilot", "full_market"):
            key = f"{profile}_{provider}_v1"
            await db.execute(
                text(
                    "INSERT INTO scheduler_control(scheduler_key,provider,slot_id,"
                    "scheduled_local_time,timezone,desired_state,observed_state,revision,"
                    "created_at,updated_at) VALUES (:key,:provider,'taiwan_market_window','18:00','Asia/Taipei',"
                    "'stopped','stopped',1,now(),now()) ON CONFLICT (scheduler_key) DO NOTHING"
                ),
                {"key": key, "provider": provider},
            )
            for dataset in datasets:
                await db.execute(
                    text(
                        "INSERT INTO scheduler_dataset(scheduler_key,dataset_key) VALUES (:key,:ds)"
                        " ON CONFLICT DO NOTHING"
                    ),
                    {"key": key, "ds": dataset},
                )
    await db.execute(
        text(
            "INSERT INTO source_client(client_id,name,source_name,key_hash,allowed_datasets,"
            "revoked_at,created_at,updated_at) VALUES (:id,:name,:source,:name,"
            "CAST(:scope AS jsonb),CAST(:revoked AS timestamptz),now(),now())"
        ),
        [
            {
                "id": ids["active"],
                "name": "active-td",
                "source": "twelve_data",
                "scope": '["us_equity_eod", "hk_equity_eod"]',
                "revoked": None,
            },
            {
                "id": ids["revoked"],
                "name": "revoked-td",
                "source": "twelve_data",
                "scope": '["us_equity_eod", "hk_equity_eod"]',
                "revoked": datetime(2026, 10, 13, tzinfo=timezone.utc),
            },
            {
                "id": ids["factset"],
                "name": "active-factset",
                "source": "factset",
                "scope": '["hk_equity_eod"]',
                "revoked": None,
            },
        ],
    )
    await db.execute(
        text(
            "INSERT INTO provider_work(work_id,environment,provider,stable_key,kind,dataset_key,"
            "scope,state,created_at) VALUES (:id,'local','twelve_data','legacy-hk','daily',"
            "'hk_equity_eod','{}','queued',now())"
        ),
        {"id": ids["work"]},
    )
    await db.execute(
        text(
            "INSERT INTO missing_delivery_alert(alert_id,dataset_key,source,schema_id,"
            "schema_version,execution_profile,expected_data_date,status,first_detected_at,"
            "last_detected_at,details) VALUES (:id,:ds,'twelve_data','market_eod',1,'full_market',"
            ":day,:status,now(),now(),CAST(:details AS jsonb))"
        ),
        [
            {
                "id": ids["open"],
                "ds": "hk_equity_eod",
                "day": date(2026, 10, 15),
                "status": "open",
                "details": '{"code": "FULL_MARKET_INCOMPLETE"}',
            },
            {
                "id": ids["done"],
                "ds": "hk_equity_eod",
                "day": date(2026, 10, 14),
                "status": "resolved",
                "details": '{"code": "FULL_MARKET_INCOMPLETE"}',
            },
            {
                "id": ids["us"],
                "ds": "us_equity_eod",
                "day": date(2026, 10, 15),
                "status": "open",
                "details": '{"code": "FULL_MARKET_INCOMPLETE"}',
            },
        ],
    )
    return ids


@pytest.mark.asyncio
async def test_retirement_refuses_unsettled_work_then_removes_only_the_retired_pair():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "findb_retire_td_hk"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        async with engine.begin() as db:
            ids = await _seed(db)

        with pytest.raises(subprocess.CalledProcessError) as refused:
            await run_alembic(url, HEAD)
        assert "unsettled evidence" in refused.value.stderr
        async with engine.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM scheduler_dataset WHERE dataset_key='hk_equity_eod' "
                        "AND scheduler_key LIKE '%twelve_data%'"
                    )
                )
                == 2
            )

        async with engine.begin() as db:
            await db.execute(
                text("UPDATE provider_work SET state='failed' WHERE work_id=:id"),
                {"id": ids["work"]},
            )
        await run_alembic(url, HEAD)

        async with engine.connect() as db:
            scopes = (
                await db.execute(
                    text(
                        "SELECT scheduler_key, dataset_key FROM scheduler_dataset WHERE "
                        "scheduler_key LIKE '%twelve_data_v1' OR scheduler_key LIKE '%factset_v1' "
                        "ORDER BY 1, 2"
                    )
                )
            ).all()
            assert [tuple(row) for row in scopes] == [
                ("full_market_factset_v1", "hk_equity_eod"),
                ("full_market_twelve_data_v1", "us_equity_eod"),
                ("pilot_factset_v1", "hk_equity_eod"),
                ("pilot_twelve_data_v1", "us_equity_eod"),
            ]
            credentials = dict(
                (
                    await db.execute(
                        text(
                            "SELECT name, allowed_datasets FROM source_client "
                            "WHERE name IN ('active-td', 'revoked-td', 'active-factset')"
                        )
                    )
                )
                .tuples()
                .all()
            )
            assert credentials == {
                "active-td": ["us_equity_eod"],
                "revoked-td": ["us_equity_eod", "hk_equity_eod"],
                "active-factset": ["hk_equity_eod"],
            }
            config = await db.scalar(
                text("SELECT config FROM dataset_registry WHERE dataset_key='hk_equity_eod'")
            )
            assert config == {
                "allowed_sources": ["factset", "future_hk_source"],
                "source_precedence": ["twelve_data"],
            }
            alerts = {
                row.alert_id: row
                for row in await db.execute(
                    text(
                        "SELECT alert_id, status, resolved_at, details FROM missing_delivery_alert"
                    )
                )
            }
            assert alerts[ids["open"]].status == "resolved"
            assert alerts[ids["open"]].resolved_at is not None
            assert alerts[ids["open"]].details == {
                "code": "FULL_MARKET_INCOMPLETE",
                "resolution": {"code": "FEED_RETIRED", "execution_profile": "full_market"},
            }
            assert alerts[ids["done"]].details == {"code": "FULL_MARKET_INCOMPLETE"}
            assert alerts[ids["us"]].status == "open"
            assert (
                await db.scalar(
                    text("SELECT state FROM provider_work WHERE work_id=:id"), {"id": ids["work"]}
                )
                == "failed"
            )
