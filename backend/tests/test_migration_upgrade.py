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

BACKEND_ROOT = Path(__file__).resolve().parents[1]
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
        cwd=BACKEND_ROOT,
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
            await connection.execute(
                text("""
                    INSERT INTO dataset_registry (
                        dataset_key, name, asset_class, market, frequency,
                        is_active, config, created_at, updated_at
                    )
                    VALUES (
                        'us_equity_eod', 'US Equity', 'equity', 'US', 'daily',
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
                            "defaults": {"currency": "CAD"},
                        }
                    )
                },
            )
            await connection.execute(
                text("""
                    INSERT INTO trading_calendar (id, market, trade_date, is_open)
                    VALUES (:id, 'TW', DATE '2026-01-01', false)
                """),
                {"id": uuid4()},
            )
            for index, (source_name, allowed_datasets) in enumerate(
                (
                    ("Twelve_Data", None),
                    ("finlab", ["tw_equity_eod", "retired_dataset"]),
                    ("shioaji", None),
                    ("bloomberg", None),
                ),
                start=1,
            ):
                await connection.execute(
                    text("""
                        INSERT INTO source_client (
                            client_id, name, source_name, key_hash, allowed_datasets,
                            rate_limit_requests, rate_limit_window, created_at, updated_at
                        ) VALUES (
                            :client_id, :name, :source_name, :key_hash,
                            CAST(:allowed_datasets AS jsonb), 100, 60, now(), now()
                        )
                    """),
                    {
                        "client_id": uuid4(),
                        "name": f"migration-source-{index}",
                        "source_name": source_name,
                        "key_hash": str(index) * 64,
                        "allowed_datasets": (
                            None if allowed_datasets is None else json.dumps(allowed_datasets)
                        ),
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

        # Continue through the pre-cutover audit schema so the migration is
        # exercised with a legitimate NULL dataset_key audit attempt.  Such
        # rejected/unauthenticated attempts are intentionally out of scope.
        await _run_alembic(database_url, "c3d4e5f6a7b8")
        null_attempt_id = uuid4()
        unknown_scheduler_key = "legacy_unknown_scheduler_v1"
        coverage_run_ids: dict[str, object] = {}
        coverage_payloads = {
            "valid": {
                "batch": {
                    "data_date": "2026-07-21",
                    "delivery_mode": "backfill",
                    "coverage_start_date": "2026-07-20",
                    "coverage_end_date": "2026-07-21",
                }
            },
            "malformed": {
                "batch": {
                    "data_date": "2026-07-21",
                    "delivery_mode": "backfill",
                    "coverage_start_date": "not-a-date",
                    "coverage_end_date": "2026-07-21",
                }
            },
            "missing": {
                "batch": {
                    "data_date": "2026-07-21",
                    "delivery_mode": "backfill",
                }
            },
            "expired": {
                "batch": {
                    "data_date": "2026-07-21",
                    "delivery_mode": "backfill",
                    "coverage_start_date": "2026-07-20",
                    "coverage_end_date": "2026-07-21",
                }
            },
        }
        async with target_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO ingestion_attempt (
                        attempt_id, source_client_id, run_id, dataset_key, source,
                        status, created_at, updated_at
                    ) VALUES (
                        :attempt_id, NULL, NULL, NULL, NULL,
                        'rejected', now(), now()
                    )
                    """
                ),
                {"attempt_id": null_attempt_id},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO scheduler_control (
                        scheduler_key, provider, slot_id, scheduled_local_time,
                        timezone, dataset_keys, desired_state, observed_state,
                        revision, created_at, updated_at
                    ) VALUES (
                        :scheduler_key, 'legacy_provider', 'legacy_unknown', TIME '00:00:00',
                        'UTC', '["retired_dataset"]'::jsonb, 'stopped', 'stopped',
                        1, now(), now()
                    )
                    """
                ),
                {"scheduler_key": unknown_scheduler_key},
            )
            for label, payload in coverage_payloads.items():
                run_id = uuid4()
                raw_payload_id = uuid4()
                coverage_run_ids[label] = run_id
                await connection.execute(
                    text(
                        """
                        INSERT INTO raw.market_payload (
                            raw_payload_id, source_client_id, dataset_key, source,
                            request_key, idempotency_key, schema_id, schema_version,
                            payload_sha256, payload, fetched_at, expire_at, run_id, created_at
                        ) VALUES (
                            :raw_payload_id, NULL, 'tw_equity_eod', 'finlab',
                            :request_key, :idempotency_key, 'market_eod', 1,
                            NULL, CAST(:payload AS jsonb), now(),
                            CASE
                                WHEN :expired THEN now() - interval '1 day'
                                ELSE now() + interval '30 days'
                            END,
                            :run_id, now()
                        )
                        """
                    ),
                    {
                        "raw_payload_id": raw_payload_id,
                        "request_key": f"migration-coverage-{label}",
                        "idempotency_key": f"migration-coverage-{label}",
                        "payload": json.dumps(payload),
                        "run_id": run_id,
                        "expired": label == "expired",
                    },
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO ingestion_run (
                            run_id, dataset_key, source, raw_payload_id, request_key,
                            schema_id, schema_version, batch_data_date, delivery_mode,
                            is_rerun, raw_records, status, total_records, success_records,
                            failed_records, attempt_count, max_attempts, created_at
                        ) VALUES (
                            :run_id, 'tw_equity_eod', 'finlab', :raw_payload_id, :request_key,
                            'market_eod', 1, DATE '2026-07-21', 'backfill',
                            false, 0, 'completed', 0, 0, 0, 0, 5, now()
                        )
                        """
                    ),
                    {
                        "run_id": run_id,
                        "raw_payload_id": raw_payload_id,
                        "request_key": f"migration-coverage-{label}",
                    },
                )
                if label == "valid":
                    await connection.execute(
                        text(
                            """
                            INSERT INTO raw.market_payload (
                                raw_payload_id, source_client_id, dataset_key, source,
                                request_key, idempotency_key, schema_id, schema_version,
                                payload_sha256, payload, fetched_at, expire_at, run_id, created_at
                            ) VALUES (
                                :raw_payload_id, NULL, 'tw_equity_eod', 'finlab',
                                :request_key, :idempotency_key, 'market_eod', 1,
                                NULL, CAST(:payload AS jsonb), now(), now() + interval '30 days',
                                :run_id, now() + interval '1 hour'
                            )
                            """
                        ),
                        {
                            "raw_payload_id": uuid4(),
                            "request_key": "migration-coverage-valid-fallback",
                            "idempotency_key": "migration-coverage-valid-fallback",
                            "payload": json.dumps(
                                {
                                    "batch": {
                                        "data_date": "2026-07-21",
                                        "delivery_mode": "backfill",
                                        "coverage_start_date": "2026-07-19",
                                        "coverage_end_date": "2026-07-21",
                                    }
                                }
                            ),
                            "run_id": run_id,
                        },
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
            cleanup_index_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'ingestion_run'
                      AND indexname = 'idx_run_raw_payload'
                    """)
            )
            attempt_table = await connection.scalar(
                text("SELECT to_regclass('public.ingestion_attempt')")
            )
            lineage_column_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE (table_schema, table_name) IN (
                        ('public', 'ingestion_run'),
                        ('raw', 'market_payload')
                    )
                      AND column_name IN ('schema_id', 'schema_version')
                    """)
            )
            contract_config = await connection.scalar(
                text("""
                    SELECT config
                    FROM dataset_registry
                    WHERE dataset_key = 'tw_equity_eod'
                    """)
            )
            default_monitor_config = await connection.scalar(
                text("""
                        SELECT config->'delivery_expectation'->'missing_delivery'
                        FROM dataset_registry
                        WHERE dataset_key = 'tw_etf_eod'
                        """)
            )
            us_equity_contract_config = await connection.scalar(
                text("""
                        SELECT config
                        FROM dataset_registry
                        WHERE dataset_key = 'us_equity_eod'
                        """)
            )
            credential_scopes = (
                await connection.execute(
                    text("""
                        SELECT source_name, allowed_datasets
                        FROM source_client
                        WHERE name LIKE 'migration-source-%'
                        ORDER BY source_name
                    """)
                )
            ).all()
            null_attempt_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM ingestion_attempt "
                    "WHERE attempt_id = :attempt_id AND dataset_key IS NULL"
                ),
                {"attempt_id": null_attempt_id},
            )
            unknown_scheduler_count = await connection.scalar(
                text("SELECT count(*) FROM scheduler_control WHERE scheduler_key = :scheduler_key"),
                {"scheduler_key": unknown_scheduler_key},
            )
            delivery_column_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND (
                        (table_name = 'ingestion_attempt' AND column_name = 'failure_details')
                        OR
                        (table_name = 'ingestion_run' AND column_name IN (
                            'batch_data_date', 'delivery_mode', 'policy_outcome',
                            'policy_details', 'is_rerun', 'coverage_start_date',
                            'coverage_end_date'
                        ))
                      )
                    """)
            )
            coverage_rows = {}
            for label, run_id in coverage_run_ids.items():
                coverage_rows[label] = (
                    await connection.execute(
                        text(
                            """
                            SELECT coverage_start_date, coverage_end_date
                            FROM ingestion_run
                            WHERE run_id = :run_id
                            """
                        ),
                        {"run_id": run_id},
                    )
                ).one()
            baseline_index_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'ingestion_run'
                      AND indexname = 'idx_run_delivery_policy_baseline'
                    """)
            )
            missing_alert_table = await connection.scalar(
                text("SELECT to_regclass('public.missing_delivery_alert')")
            )
            missing_alert_index_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'missing_delivery_alert'
                      AND indexname IN (
                        'idx_missing_delivery_status_detected',
                        'idx_missing_delivery_dataset_source'
                      )
                    """)
            )
            futures_contract_field_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'futures_continuous_eod'
                      AND column_name IN (
                        'open_interest', 'active_contract_code', 'roll_adjustment'
                      )
                    """)
            )
            active_outbox_index_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'normalization_outbox'
                      AND indexname = 'uq_normalization_outbox_active_job'
                      AND indexdef ILIKE 'CREATE UNIQUE INDEX%'
                    """)
            )
            migrated_calendar = (
                await connection.execute(
                    text("""
                        SELECT day_status, source_kind, revision
                        FROM trading_calendar
                        WHERE market = 'TW' AND trade_date = DATE '2026-01-01'
                    """)
                )
            ).one()
            calendar_market_count = await connection.scalar(
                text("SELECT count(*) FROM calendar_market")
            )
            calendar_revision_timezone_column_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'calendar_year_revision'
                      AND column_name = 'timezone'
                    """)
            )
        assert column_count == len(SOURCE_CONTROL_TABLES) * 2
        assert cleanup_index_count == 1
        assert attempt_table == "ingestion_attempt"
        assert lineage_column_count == 4
        assert contract_config["schema_id"] == "market_eod"
        assert contract_config["accepted_schema_versions"] == [1]
        assert contract_config["current_schema_version"] == 1
        assert contract_config["schema_enforcement"] == "enforce"
        assert contract_config["allowed_sources"] == ["finlab"]
        assert contract_config["defaults"] == {
            "market": "TW",
            "asset_class": "equity",
            "currency": "TWD",
        }
        assert contract_config["custom"] == "preserve"
        assert delivery_column_count == 8
        assert tuple(
            value.isoformat() if value is not None else None for value in coverage_rows["valid"]
        ) == ("2026-07-20", "2026-07-21")
        assert coverage_rows["malformed"] == (None, None)
        assert coverage_rows["missing"] == (None, None)
        assert coverage_rows["expired"] == (None, None)
        assert baseline_index_count == 1
        assert missing_alert_table == "missing_delivery_alert"
        assert missing_alert_index_count == 2
        assert futures_contract_field_count == 3
        assert active_outbox_index_count == 1
        assert migrated_calendar == ("closed", "observed_ingestion", 0)
        assert calendar_market_count == 13
        assert calendar_revision_timezone_column_count == 1
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
        assert default_monitor_config is None
        assert us_equity_contract_config["schema_id"] == "market_eod"
        assert us_equity_contract_config["accepted_schema_versions"] == [1]
        assert us_equity_contract_config["current_schema_version"] == 1
        assert us_equity_contract_config["schema_enforcement"] == "enforce"
        assert us_equity_contract_config["allowed_sources"] == ["twelve_data"]
        assert us_equity_contract_config["defaults"] == {
            "market": "US",
            "asset_class": "equity",
            "currency": "USD",
        }
        assert us_equity_contract_config["delivery_expectation"]["delivery_mode"] == "incremental"
        assert us_equity_contract_config["custom"] == "preserve"
        assert credential_scopes == [
            ("bloomberg", []),
            ("finlab", ["tw_equity_eod"]),
            ("shioaji", ["tw_equity_minute", "tw_etf_minute"]),
            ("twelve_data", ["us_equity_eod"]),
        ]
        assert null_attempt_count == 1
        assert unknown_scheduler_count == 0

        await _run_alembic(database_url, "08b9c0d1e2f3", command="downgrade")
        async with target_engine.connect() as connection:
            futures_contract_field_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'futures_continuous_eod'
                      AND column_name IN (
                        'open_interest', 'active_contract_code', 'roll_adjustment'
                      )
                    """)
            )
            cleanup_index_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = 'ingestion_run'
                      AND indexname = 'idx_run_raw_payload'
                    """)
            )
            attempt_table = await connection.scalar(
                text("SELECT to_regclass('public.ingestion_attempt')")
            )
            assert futures_contract_field_count == 0
            lineage_column_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE (table_schema, table_name) IN (
                        ('public', 'ingestion_run'),
                        ('raw', 'market_payload')
                    )
                      AND column_name IN ('schema_id', 'schema_version')
                    """)
            )
            contract_config = await connection.scalar(
                text("""
                    SELECT config
                    FROM dataset_registry
                    WHERE dataset_key = 'tw_equity_eod'
                    """)
            )
            delivery_column_count = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND (
                        (table_name = 'ingestion_attempt' AND column_name = 'failure_details')
                        OR
                        (table_name = 'ingestion_run' AND column_name IN (
                            'batch_data_date', 'delivery_mode', 'policy_outcome',
                            'policy_details', 'is_rerun', 'coverage_start_date',
                            'coverage_end_date'
                        ))
                      )
                    """)
            )
            missing_alert_table = await connection.scalar(
                text("SELECT to_regclass('public.missing_delivery_alert')")
            )
        assert cleanup_index_count == 0
        assert attempt_table is None
        assert lineage_column_count == 0
        assert "schema_id" not in contract_config
        assert "accepted_schema_versions" not in contract_config
        assert "current_schema_version" not in contract_config
        assert "schema_enforcement" not in contract_config
        assert "allowed_sources" not in contract_config
        assert "defaults" not in contract_config
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
