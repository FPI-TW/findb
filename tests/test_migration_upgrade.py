"""Regression tests for upgrading databases created by earlier branch revisions."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://findb:findb@localhost:5435/findb_test",
)
SOURCE_CONTROL_TABLES = (
    "market_data_eod",
    "corporate_action",
    "macro_observation",
    "futures_contract",
    "futures_continuous_eod",
)


async def _run_alembic(
    database_url: str,
    revision: str,
    *,
    command: str = "upgrade",
) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", command, revision],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_upgrade_from_early_f7_repairs_schema() -> None:
    base_url = make_url(BASE_DATABASE_URL)
    database_name = f"findb_migration_{uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    target_engine = None

    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        await _run_alembic(database_url, "f7a8b9c0d1e2")
        target_engine = create_async_engine(database_url)
        async with target_engine.begin() as connection:
            await connection.execute(
                text("""
                    INSERT INTO dataset_registry (
                        dataset_key, name, asset_class, market, frequency,
                        is_active, config, created_at, updated_at
                    )
                    VALUES (
                        'tw_equity_eod', 'TW Equity', 'equity', 'TW', 'daily',
                        true, CAST(:config AS jsonb), now(), now()
                    )
                    ON CONFLICT (dataset_key) DO UPDATE
                    SET config = EXCLUDED.config
                    """),
                {
                    "config": json.dumps(
                        {
                            "source_format": "legacy",
                            "custom": "preserve",
                            "schema_id": "production_contract",
                            "accepted_schema_versions": [99],
                            "defaults": {"market": "US"},
                            "delivery_expectation": {
                                "delivery_mode": "full_snapshot",
                                "freshness_hours": 72,
                                "minimum_record_count": 1777,
                                "maximum_count_drop_ratio": 0.2,
                                "operator_note": "preserve",
                                "missing_delivery": {
                                    "action": "warn",
                                    "expected_sources": ["operator_feed"],
                                },
                            },
                        }
                    )
                },
            )
            for table_name in SOURCE_CONTROL_TABLES:
                await connection.execute(
                    text(
                        f'ALTER TABLE "{table_name}" '
                        "DROP COLUMN IF EXISTS source_fetched_at, "
                        "DROP COLUMN IF EXISTS source_priority"
                    )
                )

        await _run_alembic(database_url, "head")

        async with target_engine.connect() as connection:
            column_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = ANY(:table_names)
                      AND column_name IN ('source_priority', 'source_fetched_at')
                    """),
                {"table_names": list(SOURCE_CONTROL_TABLES)},
            )
            cleanup_index_count = await connection.scalar(text("""
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'ingestion_run'
                      AND indexname = 'idx_run_raw_payload'
                    """))
            attempt_table = await connection.scalar(
                text("SELECT to_regclass('public.ingestion_attempt')")
            )
            lineage_column_count = await connection.scalar(text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE (table_schema, table_name) IN (
                        ('public', 'ingestion_run'),
                        ('raw', 'market_payload')
                    )
                      AND column_name IN ('schema_id', 'schema_version')
                    """))
            contract_config = await connection.scalar(text("""
                    SELECT config
                    FROM dataset_registry
                    WHERE dataset_key = 'tw_equity_eod'
                    """))
            default_monitor_config = await connection.scalar(text("""
                    SELECT config->'delivery_expectation'->'missing_delivery'
                    FROM dataset_registry
                    WHERE dataset_key = 'tw_etf_eod'
                    """))
            delivery_column_count = await connection.scalar(text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND (
                        (table_name = 'ingestion_attempt' AND column_name = 'failure_details')
                        OR
                        (table_name = 'ingestion_run' AND column_name IN (
                            'batch_data_date', 'delivery_mode', 'policy_outcome',
                            'policy_details', 'is_rerun'
                        ))
                      )
                    """))
            baseline_index_count = await connection.scalar(text("""
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'ingestion_run'
                      AND indexname = 'idx_run_delivery_policy_baseline'
                    """))
            missing_alert_table = await connection.scalar(
                text("SELECT to_regclass('public.missing_delivery_alert')")
            )
            missing_alert_index_count = await connection.scalar(text("""
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'missing_delivery_alert'
                      AND indexname IN (
                        'idx_missing_delivery_status_detected',
                        'idx_missing_delivery_dataset_source'
                      )
                    """))
            futures_contract_field_count = await connection.scalar(text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'futures_continuous_eod'
                      AND column_name IN (
                        'open_interest', 'active_contract_code', 'roll_adjustment'
                      )
                    """))
        assert column_count == len(SOURCE_CONTROL_TABLES) * 2
        assert cleanup_index_count == 1
        assert attempt_table == "ingestion_attempt"
        assert lineage_column_count == 4
        assert contract_config["schema_id"] == "production_contract"
        assert contract_config["accepted_schema_versions"] == [99]
        assert contract_config["schema_enforcement"] == "audit"
        assert contract_config["defaults"] == {
            "market": "US",
            "asset_class": "equity",
            "currency": "TWD",
        }
        assert contract_config["custom"] == "preserve"
        assert delivery_column_count == 6
        assert baseline_index_count == 1
        assert missing_alert_table == "missing_delivery_alert"
        assert missing_alert_index_count == 2
        assert futures_contract_field_count == 3
        expectation = contract_config["delivery_expectation"]
        assert expectation["freshness_hours"] == 72
        assert expectation["minimum_record_count"] == 1777
        assert expectation["maximum_count_drop_ratio"] == 0.2
        assert expectation["operator_note"] == "preserve"
        assert expectation["record_count"]["action"] == "warn"
        assert expectation["latest_date"]["timezone"] == "Asia/Taipei"
        assert expectation["missing_delivery"] == {
            "action": "warn",
            "expected_sources": ["operator_feed"],
        }
        assert default_monitor_config == {
            "action": "disabled",
            "expected_sources": [],
        }

        await _run_alembic(database_url, "08b9c0d1e2f3", command="downgrade")
        async with target_engine.connect() as connection:
            futures_contract_field_count = await connection.scalar(text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'futures_continuous_eod'
                      AND column_name IN (
                        'open_interest', 'active_contract_code', 'roll_adjustment'
                      )
                    """))
            cleanup_index_count = await connection.scalar(text("""
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'ingestion_run'
                      AND indexname = 'idx_run_raw_payload'
                    """))
            attempt_table = await connection.scalar(
                text("SELECT to_regclass('public.ingestion_attempt')")
            )
            assert futures_contract_field_count == 0
            lineage_column_count = await connection.scalar(text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE (table_schema, table_name) IN (
                        ('public', 'ingestion_run'),
                        ('raw', 'market_payload')
                    )
                      AND column_name IN ('schema_id', 'schema_version')
                    """))
            contract_config = await connection.scalar(text("""
                    SELECT config
                    FROM dataset_registry
                    WHERE dataset_key = 'tw_equity_eod'
                    """))
            delivery_column_count = await connection.scalar(text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND (
                        (table_name = 'ingestion_attempt' AND column_name = 'failure_details')
                        OR
                        (table_name = 'ingestion_run' AND column_name IN (
                            'batch_data_date', 'delivery_mode', 'policy_outcome',
                            'policy_details', 'is_rerun'
                        ))
                      )
                    """))
            missing_alert_table = await connection.scalar(
                text("SELECT to_regclass('public.missing_delivery_alert')")
            )
        assert cleanup_index_count == 0
        assert attempt_table is None
        assert lineage_column_count == 0
        assert contract_config["schema_id"] == "production_contract"
        assert contract_config["custom"] == "preserve"
        assert delivery_column_count == 0
        assert missing_alert_table is None
        assert contract_config["delivery_expectation"]["operator_note"] == "preserve"
    finally:
        if target_engine is not None:
            await target_engine.dispose()
        async with admin_engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :db"),
                {"db": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await admin_engine.dispose()
