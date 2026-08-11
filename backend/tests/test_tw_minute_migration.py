import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
BASE_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://findb:findb@localhost:5435/findb_test"
)


async def _run_alembic(database_url: str, revision: str, command: str = "upgrade") -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", command, revision],
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Alembic {command} {revision} failed:\n{result.stdout}\n{result.stderr}")


async def _run_alembic_capture(
    database_url: str, revision: str, command: str = "upgrade"
) -> subprocess.CompletedProcess[str]:
    """Run Alembic without converting a deliberate migration failure to pytest.fail."""

    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    return await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", command, revision],
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_tw_minute_migration_is_single_linear_head():
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    scripts = ScriptDirectory.from_config(config)

    foundation = scripts.get_revision("a9b0c1d2e3f4")
    activation = scripts.get_revision("b0c1d2e3f4a5")
    scheduler_control = scripts.get_revision("c1d2e3f4a5b6")
    scheduler_definition = scripts.get_revision("d2e3f4a5b6c7")
    finlab_policy = scripts.get_revision("e3f4a5b6c7d8")
    minute_freshness = scripts.get_revision("f4a5b6c7d8e9")
    minute_deadline = scripts.get_revision("c3d4e5f6a7b8")
    stage_four_feeds = scripts.get_revision("d4e5f6a7b8c9")
    assert foundation is not None
    assert foundation.down_revision == "f8a9b0c1d2e3"
    assert activation is not None
    assert activation.down_revision == "a9b0c1d2e3f4"
    assert scheduler_control is not None
    assert scheduler_control.down_revision == "b0c1d2e3f4a5"
    assert scheduler_definition is not None
    assert scheduler_definition.down_revision == "c1d2e3f4a5b6"
    assert finlab_policy is not None
    assert finlab_policy.down_revision == "d2e3f4a5b6c7"
    assert minute_freshness is not None
    assert minute_freshness.down_revision == "e3f4a5b6c7d8"
    assert minute_deadline is not None
    assert minute_deadline.down_revision == "b2c3d4e5f6a7"
    assert stage_four_feeds is not None
    assert stage_four_feeds.down_revision == "c3d4e5f6a7b8"
    assert scripts.get_heads() == ["d4e5f6a7b8c9"]


def test_minute_migration_downgrade_preserves_policy_provenance():
    migration_path = (
        BACKEND_ROOT / "migrations" / "versions" / "f4a5b6c7d8e9_add_minute_delivery_freshness.py"
    )
    migration_source = migration_path.read_text(encoding="utf-8")
    assert "config #- '{delivery_expectation}'" not in migration_source
    assert "policy provenance is not stored" in migration_source.lower()
    assert "ADD COLUMN IF NOT EXISTS snapshot_id" in migration_source
    assert "run.snapshot_id IS NULL" in migration_source
    assert "DO $$" not in migration_source


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_location", ("scheduler", "registry"))
async def test_slot_identity_migration_rejects_unknown_ids_before_mutation(
    invalid_location: str,
) -> None:
    """Unknown governed IDs fail before A1 can rename or rewrite any row."""

    base_url = make_url(BASE_DATABASE_URL)
    database_name = f"findb_slot_guard_{uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    target_engine = None
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        await _run_alembic(database_url, "f4a5b6c7d8e9")
        target_engine = create_async_engine(database_url)
        async with target_engine.begin() as connection:
            if invalid_location == "scheduler":
                await connection.execute(
                    text(
                        """
                        UPDATE scheduler_control
                        SET slot_id = 'operator_unknown_slot'
                        WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        """
                    )
                )
            else:
                await connection.execute(
                    text(
                        """
                        UPDATE dataset_registry
                        SET config = jsonb_set(
                            COALESCE(config, '{}'::jsonb),
                            '{delivery_expectation,schedule}',
                            CAST(:schedule AS jsonb),
                            true
                        )
                        WHERE dataset_key = 'tw_equity_eod'
                        """
                    ),
                    {
                        "schedule": json.dumps(
                            {
                                "enabled": True,
                                "slot_id": "operator_unknown_slot",
                                "local_time": "06:30:00",
                                "timezone": "Asia/Taipei",
                                "expected_sources": [],
                            }
                        )
                    },
                )

            before = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, slot_id, scheduled_local_time, timezone
                        FROM scheduler_control
                        WHERE scheduler_key = 'finlab_tw_1430_tw_equity_eod'
                        """
                    )
                )
            ).one()

        failed = await _run_alembic_capture(database_url, "head")
        assert failed.returncode != 0
        assert "unsupported" in f"{failed.stdout}\n{failed.stderr}"

        async with target_engine.connect() as connection:
            after = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, slot_id, scheduled_local_time, timezone
                        FROM scheduler_control
                        WHERE scheduler_key = 'finlab_tw_1430_tw_equity_eod'
                        """
                    )
                )
            ).one()
            assert after == before
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "f4a5b6c7d8e9"
            )
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


@pytest.mark.asyncio
async def test_slot_identity_migration_accepts_mixed_ids_and_preserves_custom_times() -> None:
    """Known mixed old/canonical rows converge without overwriting custom clocks."""

    base_url = make_url(BASE_DATABASE_URL)
    database_name = f"findb_slot_mixed_{uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    target_engine = None
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        await _run_alembic(database_url, "f4a5b6c7d8e9")
        target_engine = create_async_engine(database_url)
        async with target_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE scheduler_control
                    SET slot_id = 'us_0600',
                        scheduled_local_time = TIME '05:45:00',
                        timezone = 'UTC'
                    WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    UPDATE scheduler_control
                    SET slot_id = 'taiwan_market_window'
                    WHERE scheduler_key = 'shioaji_tw_pilot_v1'
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO dataset_registry (
                        dataset_key, name, asset_class, market, frequency,
                        is_active, config, created_at, updated_at
                    ) VALUES (
                        'us_equity_eod', 'US equity EOD', 'equity', 'US', 'daily',
                        true,
                        jsonb_build_object(
                            'delivery_expectation',
                            jsonb_build_object('schedule', CAST(:schedule AS jsonb))
                        ),
                        now(), now()
                    )
                    ON CONFLICT (dataset_key) DO UPDATE
                    SET config = EXCLUDED.config, updated_at = now()
                    """
                ),
                {
                    "schedule": json.dumps(
                        {
                            "enabled": True,
                            "slot_id": "us_0600",
                            "local_time": "05:45:00",
                            "timezone": "UTC",
                            "expected_sources": ["twelve_data"],
                        }
                    )
                },
            )

        await _run_alembic(database_url, "head")
        async with target_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, slot_id, scheduled_local_time, timezone
                        FROM scheduler_control
                        WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        """
                    )
                )
            ).one()
            assert row == (
                "twelve_data_us_common_stocks_daily_v1",
                "western_markets_window",
                time(5, 45),
                "UTC",
            )
            schedule = await connection.scalar(
                text(
                    """
                    SELECT config->'delivery_expectation'->'schedule'
                    FROM dataset_registry
                    WHERE dataset_key = 'us_equity_eod'
                    """
                )
            )
            assert schedule["slot_id"] == "western_markets_window"
            assert schedule["local_time"] == "05:45:00"
            assert (
                await connection.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM scheduler_control
                        WHERE slot_id IN ('us_0600', 'global_0815', 'tw_1430', 'asia_1630')
                        """
                    )
                )
                == 0
            )

        await _run_alembic(database_url, "f4a5b6c7d8e9", command="downgrade")
        async with target_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, slot_id, scheduled_local_time, timezone
                        FROM scheduler_control
                        WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        """
                    )
                )
            ).one()
            assert row == (
                "twelve_data_us_common_stocks_daily_v1",
                "us_0600",
                time(5, 45),
                "UTC",
            )
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


@pytest.mark.asyncio
async def test_minute_identity_migration_backfills_legacy_runs_safely():
    """The f4 upgrade recovers retained identity without blocking old runs."""
    base_url = make_url(BASE_DATABASE_URL)
    database_name = f"findb_minute_identity_{uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    target_engine = None
    database_created = False

    valid_with_raw_id = uuid4()
    valid_with_run_id = uuid4()
    legacy_without_raw = uuid4()
    malformed_raw_run = uuid4()
    non_minute_run = uuid4()
    raw_with_raw_id = uuid4()
    raw_with_run_id = uuid4()
    malformed_raw_id = uuid4()

    async def insert_run(connection, run_id, *, dataset_key, delivery_mode, raw_payload_id=None):
        await connection.execute(
            text(
                """
                INSERT INTO ingestion_run (
                    run_id, dataset_key, source, raw_payload_id, request_key,
                    schema_id, schema_version, batch_data_date, delivery_mode,
                    raw_records, status, completed_at, total_records,
                    success_records, failed_records, created_at
                ) VALUES (
                    :run_id, :dataset_key, 'shioaji', :raw_payload_id, :request_key,
                    'market_minute', 1, '2026-07-22', :delivery_mode,
                    1, 'completed', now(), 1, 1, 0, now()
                )
                """
            ),
            {
                "run_id": run_id,
                "dataset_key": dataset_key,
                "raw_payload_id": raw_payload_id,
                "request_key": f"migration:{run_id}",
                "delivery_mode": delivery_mode,
            },
        )

    async def insert_raw(connection, raw_id, run_id, payload, *, expired=False):
        await connection.execute(
            text(
                """
                INSERT INTO raw.market_payload (
                    raw_payload_id, dataset_key, source, request_key,
                    idempotency_key, schema_id, schema_version, payload,
                    fetched_at, expire_at, run_id, created_at
                ) VALUES (
                    :raw_id, 'tw_equity_minute', 'shioaji', :request_key,
                    :idempotency_key, 'market_minute', 1, CAST(:payload AS jsonb),
                    now(), :expire_at, :run_id, now()
                )
                """
            ),
            {
                "raw_id": raw_id,
                "run_id": run_id,
                "request_key": f"raw:{raw_id}",
                "idempotency_key": f"raw:{raw_id}",
                "payload": json.dumps(payload),
                "expire_at": datetime(2020, 1, 1, tzinfo=timezone.utc)
                if expired
                else datetime(2030, 1, 1, tzinfo=timezone.utc),
            },
        )

    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        database_created = True

        await _run_alembic(database_url, "e3f4a5b6c7d8")
        target_engine = create_async_engine(database_url)
        async with target_engine.begin() as connection:
            # An operator-owned expectation is preserved byte-for-byte while
            # the absent policy on the equity dataset receives the seed.
            await connection.execute(
                text(
                    """
                    UPDATE dataset_registry
                    SET config = CAST(:config AS jsonb)
                    WHERE dataset_key = 'tw_etf_minute'
                    """
                ),
                {
                    "config": json.dumps(
                        {
                            "operator_marker": "keep-me",
                            "delivery_expectation": {
                                "delivery_mode": "sequenced_snapshot",
                                "missing_delivery": {"action": "disabled"},
                            },
                        }
                    )
                },
            )
            valid_batch = {
                "delivery_mode": "sequenced_snapshot",
                "snapshot_id": "staging-snapshot",
                "daily_update_id": "staging-update",
                "sequence": 1,
                "sequence_count": 2,
            }
            await insert_raw(
                connection,
                raw_with_raw_id,
                valid_with_raw_id,
                {"batch": valid_batch},
            )
            await insert_run(
                connection,
                valid_with_raw_id,
                dataset_key="tw_equity_minute",
                delivery_mode="sequenced_snapshot",
                raw_payload_id=raw_with_raw_id,
            )
            await insert_raw(
                connection,
                raw_with_run_id,
                valid_with_run_id,
                {"batch": {**valid_batch, "sequence": 2}},
            )
            await insert_run(
                connection,
                valid_with_run_id,
                dataset_key="tw_equity_minute",
                delivery_mode="sequenced_snapshot",
            )
            await insert_run(
                connection,
                legacy_without_raw,
                dataset_key="tw_equity_minute",
                delivery_mode="sequenced_snapshot",
            )
            await insert_raw(
                connection,
                malformed_raw_id,
                malformed_raw_run,
                {"batch": {"delivery_mode": "sequenced_snapshot", "sequence": "oops"}},
                expired=True,
            )
            await insert_run(
                connection,
                malformed_raw_run,
                dataset_key="tw_equity_minute",
                delivery_mode="sequenced_snapshot",
            )
            await insert_run(
                connection,
                non_minute_run,
                dataset_key="tw_equity_eod",
                delivery_mode="full_snapshot",
            )

        await _run_alembic(database_url, "head")
        # Re-running Alembic at head is a no-op; f4 SQL is also guarded by
        # IF NOT EXISTS/NULL-only predicates for partial deploy recovery.
        await _run_alembic(database_url, "head")

        async with target_engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT run_id, snapshot_id, daily_update_id, sequence, sequence_count
                        FROM ingestion_run
                        WHERE run_id IN (:valid_one, :valid_two, :legacy, :malformed)
                        ORDER BY run_id
                        """
                    ),
                    {
                        "valid_one": valid_with_raw_id,
                        "valid_two": valid_with_run_id,
                        "legacy": legacy_without_raw,
                        "malformed": malformed_raw_run,
                    },
                )
            ).all()
            by_id = {row.run_id: row for row in rows}
            assert by_id[valid_with_raw_id][1:] == (
                "staging-snapshot",
                "staging-update",
                1,
                2,
            )
            assert by_id[valid_with_run_id][1:] == (
                "staging-snapshot",
                "staging-update",
                2,
                2,
            )
            assert by_id[legacy_without_raw][1:] == (None, None, None, None)
            assert by_id[malformed_raw_run][1:] == (None, None, None, None)

            seeded_policy = await connection.scalar(
                text(
                    """
                    SELECT config->'delivery_expectation'->>'delivery_mode'
                    FROM dataset_registry
                    WHERE dataset_key = 'tw_equity_minute'
                    """
                )
            )
            assert seeded_policy == "sequenced_snapshot"
            operator_config = await connection.scalar(
                text("SELECT config FROM dataset_registry WHERE dataset_key = 'tw_etf_minute'")
            )
            assert operator_config["operator_marker"] == "keep-me"
            assert operator_config["delivery_expectation"] == {
                "delivery_mode": "sequenced_snapshot",
                "missing_delivery": {"action": "disabled"},
            }
            assert operator_config["schema_id"] == "market_minute"
            assert operator_config["accepted_schema_versions"] == [1]
            assert operator_config["allowed_sources"] == ["shioaji"]

        with pytest.raises(DBAPIError):
            async with target_engine.begin() as connection:
                await connection.execute(
                    text("UPDATE ingestion_run SET snapshot_id = 'partial' WHERE run_id = :run_id"),
                    {"run_id": non_minute_run},
                )
    finally:
        if target_engine is not None:
            await target_engine.dispose()
        if database_created:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name"
                    ),
                    {"name": database_name},
                )
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_definition_backfill_preserves_state_and_downgrades():
    """The scheduler definition migration is a lossless control-plane upgrade."""
    base_url = make_url(BASE_DATABASE_URL)
    database_name = f"findb_scheduler_definition_{uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    target_engine = None
    database_created = False

    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        database_created = True

        await _run_alembic(database_url, "c1d2e3f4a5b6")
        target_engine = create_async_engine(database_url)
        async with target_engine.begin() as connection:
            # Force the fresh-install shape: c1 has no EOD registry targets yet
            # (the seed runs after migrations).  d2 must materialize them before
            # creating the normalized FK association.
            await connection.execute(
                text(
                    """
                    DELETE FROM dataset_registry
                    WHERE dataset_key IN ('us_equity_eod', 'tw_equity_eod')
                    """
                )
            )
            assert (
                await connection.scalar(
                    text(
                        """
                    SELECT count(*)
                    FROM dataset_registry
                    WHERE dataset_key IN ('us_equity_eod', 'tw_equity_eod')
                    """
                    )
                )
                == 0
            )
            heartbeat = datetime(2026, 8, 4, 5, 0, tzinfo=timezone.utc)
            cycle_started = datetime(2026, 8, 4, 5, 1, tzinfo=timezone.utc)
            cycle_completed = datetime(2026, 8, 4, 5, 4, tzinfo=timezone.utc)
            await connection.execute(
                text(
                    """
                    UPDATE scheduler_control
                    SET desired_state = 'running',
                        observed_state = 'running',
                        revision = 42,
                        last_heartbeat_at = :heartbeat,
                        last_cycle_started_at = :cycle_started,
                        last_cycle_completed_at = :cycle_completed,
                        last_error = 'operator test failure'
                    WHERE scheduler_key = 'shioaji_tw_pilot_v1'
                    """
                ),
                {
                    "heartbeat": heartbeat,
                    "cycle_started": cycle_started,
                    "cycle_completed": cycle_completed,
                },
            )
            state_before = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, provider, dataset_keys,
                               desired_state, observed_state, revision,
                               last_heartbeat_at, last_cycle_started_at,
                               last_cycle_completed_at, last_error,
                               created_at, updated_at
                        FROM scheduler_control
                        ORDER BY scheduler_key
                        """
                    )
                )
            ).all()

        await _run_alembic(database_url, "head")
        async with target_engine.connect() as connection:
            definitions = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, provider, slot_id,
                               scheduled_local_time, timezone
                        FROM scheduler_control
                        ORDER BY scheduler_key
                        """
                    )
                )
            ).all()
            assert definitions == [
                (
                    "finlab_tw_equity_eod_v1",
                    "finlab",
                    "taiwan_market_window",
                    time(14, 30),
                    "Asia/Taipei",
                ),
                (
                    "shioaji_tw_pilot_v1",
                    "shioaji",
                    "taiwan_market_window",
                    time(14, 30),
                    "Asia/Taipei",
                ),
                (
                    "twelve_data_us_common_stocks_daily_v1",
                    "twelve_data",
                    "western_markets_window",
                    time(6, 30),
                    "Asia/Taipei",
                ),
            ]

            mappings = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, dataset_key
                        FROM scheduler_dataset
                        ORDER BY scheduler_key, dataset_key
                        """
                    )
                )
            ).all()
            assert mappings == [
                ("finlab_tw_equity_eod_v1", "tw_equity_eod"),
                ("shioaji_tw_pilot_v1", "tw_equity_minute"),
                ("shioaji_tw_pilot_v1", "tw_etf_minute"),
                ("twelve_data_us_common_stocks_daily_v1", "us_equity_eod"),
            ]

            registry_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT dataset_key, asset_class, market, frequency, is_active, config
                        FROM dataset_registry
                        WHERE dataset_key IN ('us_equity_eod', 'tw_equity_eod')
                        ORDER BY dataset_key
                        """
                    )
                )
            ).all()
            assert registry_rows == [
                (
                    "tw_equity_eod",
                    "equity",
                    "TW",
                    "daily",
                    True,
                    {
                        "schema_id": "market_eod",
                        "accepted_schema_versions": [1],
                        "current_schema_version": 1,
                        "schema_enforcement": "enforce",
                        "allowed_sources": ["finlab"],
                        "defaults": {
                            "market": "TW",
                            "asset_class": "equity",
                            "currency": "TWD",
                        },
                        "delivery_expectation": {
                            "missing_delivery": {
                                "action": "warn",
                                "expected_sources": ["finlab"],
                                "deadline_local_time": "17:00:00",
                            }
                        },
                    },
                ),
                (
                    "us_equity_eod",
                    "equity",
                    "US",
                    "daily",
                    True,
                    {
                        "schema_id": "market_eod",
                        "accepted_schema_versions": [1],
                        "current_schema_version": 1,
                        "schema_enforcement": "enforce",
                        "allowed_sources": ["twelve_data"],
                        "defaults": {
                            "market": "US",
                            "asset_class": "equity",
                            "currency": "USD",
                        },
                        "delivery_expectation": {
                            "missing_delivery": {
                                "action": "warn",
                                "expected_sources": ["twelve_data"],
                                "deadline_local_time": "17:00:00",
                            }
                        },
                    },
                ),
            ]

            state_after = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, provider, dataset_keys,
                               desired_state, observed_state, revision,
                               last_heartbeat_at, last_cycle_started_at,
                               last_cycle_completed_at, last_error,
                               created_at, updated_at
                        FROM scheduler_control
                        ORDER BY scheduler_key
                        """
                    )
                )
            ).all()
            expected_state_after = [
                (
                    "finlab_tw_equity_eod_v1"
                    if row[0] == "finlab_tw_1430_tw_equity_eod"
                    else row[0],
                    *row[1:],
                )
                for row in state_before
            ]
            assert state_after == expected_state_after

        await _run_alembic(database_url, "c1d2e3f4a5b6", command="downgrade")
        async with target_engine.connect() as connection:
            legacy_state = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, provider, dataset_keys,
                               desired_state, observed_state, revision,
                               last_heartbeat_at, last_cycle_started_at,
                               last_cycle_completed_at, last_error,
                               created_at, updated_at
                        FROM scheduler_control
                        ORDER BY scheduler_key
                        """
                    )
                )
            ).all()
            assert legacy_state == state_before
            assert (
                await connection.scalar(text("SELECT to_regclass('public.scheduler_dataset')"))
                is None
            )
            assert (
                await connection.scalar(
                    text(
                        """
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'scheduler_control'
                      AND column_name IN ('slot_id', 'scheduled_local_time', 'timezone')
                    """
                    )
                )
                == 0
            )
            assert (
                await connection.scalar(
                    text(
                        """
                    SELECT count(*)
                    FROM dataset_registry
                    WHERE dataset_key IN ('us_equity_eod', 'tw_equity_eod')
                    """
                    )
                )
                == 2
            )
    finally:
        if target_engine is not None:
            await target_engine.dispose()
        if database_created:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
                )
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_shioaji_pilot_activation_migration_is_reversible():
    base_url = make_url(BASE_DATABASE_URL)
    database_name = f"findb_shioaji_activation_{uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    target_engine = None

    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        await _run_alembic(database_url, "a9b0c1d2e3f4")
        target_engine = create_async_engine(database_url)
        async with target_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO dataset_registry (
                        dataset_key, name, asset_class, market, frequency,
                        is_active, config, created_at, updated_at
                        ) VALUES (
                            'tw_equity_minute', 'operator name', 'equity', 'TW', 'minute',
                            false, CAST(:operator_config AS jsonb), now(), now()
                        )
                        """
                ),
                {"operator_config": json.dumps({"operator_owned": True})},
            )

        await _run_alembic(database_url, "b0c1d2e3f4a5")
        async with target_engine.connect() as connection:
            active = await connection.execute(
                text(
                    """
                    SELECT dataset_key, is_active, config
                    FROM dataset_registry
                    WHERE dataset_key IN ('tw_equity_minute', 'tw_etf_minute')
                    ORDER BY dataset_key
                    """
                )
            )
            rows = active.all()
            assert rows[0] == ("tw_equity_minute", True, {"operator_owned": True})
            assert rows[1][0:2] == ("tw_etf_minute", True)
            assert rows[1][2]["schema_id"] == "market_minute"
            assert rows[1][2]["source_format"] == "shioaji_tw_minute"

        await _run_alembic(database_url, "a9b0c1d2e3f4", command="downgrade")
        async with target_engine.connect() as connection:
            inactive = await connection.execute(
                text(
                    """
                    SELECT dataset_key, is_active
                    FROM dataset_registry
                    WHERE dataset_key IN ('tw_equity_minute', 'tw_etf_minute')
                    ORDER BY dataset_key
                    """
                )
            )
            assert inactive.all() == [
                ("tw_equity_minute", False),
                ("tw_etf_minute", False),
            ]
    finally:
        if target_engine is not None:
            await target_engine.dispose()
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
            )
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_tw_minute_upgrade_downgrade_upgrade_round_trip():
    base_url = make_url(BASE_DATABASE_URL)
    database_name = f"findb_tw_minute_migration_{uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    target_engine = None

    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        await _run_alembic(database_url, "f8a9b0c1d2e3")
        await _run_alembic(database_url, "a9b0c1d2e3f4")
        target_engine = create_async_engine(database_url)
        async with target_engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('public.market_data_minute')"))
            assert await connection.scalar(
                text("SELECT to_regclass('public.market_data_minute_default')")
            )
            archive_chunk_columns = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'tw_minute_archive_chunk'
                      AND column_name IN (
                        'snapshot_sequence', 'chunk_sequence', 'chunk_count',
                        'instrument_checksum', 'chunk_checksum',
                        'object_key', 'object_checksum', 'object_size_bytes'
                      )
                """)
            )
            assert archive_chunk_columns == 8
            assert await connection.scalar(
                text("SELECT to_regprocedure('validate_tw_minute_archive_finalization()')")
            )
            assert await connection.scalar(
                text("SELECT to_regprocedure('guard_tw_minute_finalized_archive_chunks()')")
            )

            instruments_checksum = hashlib.sha256(b"2330").hexdigest()
            trading_dates_checksum = hashlib.sha256(b"2026-07-30").hexdigest()
            chunk_checksum = "c" * 64
            object_checksum = "d" * 64
            object_key = "minute/2026-07/chunk-1.parquet"
            manifest = {
                "schema_id": "market_minute_archive",
                "schema_version": 1,
                "release_id": "tw-2026-07-release",
                "dataset_key": "tw_equity_minute",
                "source": "tw_recorder_archive",
                "upstream_source": "shioaji",
                "overlap_precedence": "direct_daily_shioaji",
                "trading_calendar_checksum": "a" * 64,
                "coverage_start_month": "2026-07-01",
                "coverage_end_month": "2026-07-01",
                "covered_months": ["2026-07-01"],
                "expected_trading_dates": ["2026-07-30"],
                "covered_trading_dates": ["2026-07-30"],
                "trading_dates_sha256": trading_dates_checksum,
                "instruments": ["2330"],
                "instruments_sha256": instruments_checksum,
                "sequence_count": 1,
                "chunk_count": 1,
                "row_count": 1,
                "checksum_sha256": "e" * 64,
                "chunks": [
                    {
                        "snapshot_sequence": 1,
                        "chunk_sequence": 1,
                        "chunk_count": 1,
                        "row_count": 1,
                        "checksum_sha256": chunk_checksum,
                        "object": {
                            "object_key": object_key,
                            "sha256": object_checksum,
                            "byte_size": 42,
                        },
                    }
                ],
                "objects": [
                    {
                        "object_key": object_key,
                        "sha256": object_checksum,
                        "byte_size": 42,
                    }
                ],
                "release_state": "finalized",
                "finalized_at": "2026-07-31T00:00:00+00:00",
            }
            await connection.execute(
                text("""
                    INSERT INTO dataset_registry (
                        dataset_key, name, asset_class, market, frequency,
                        is_active, config, created_at, updated_at
                    ) VALUES (
                        'tw_equity_minute', 'TW minute', 'equity', 'TW', 'minute',
                        false, '{}'::jsonb, now(), now()
                    ), (
                        'tw_etf_minute', 'TW ETF minute', 'etf', 'TW', 'minute',
                        false, '{}'::jsonb, now(), now()
                    )
                """)
            )
            release_insert = text("""
                INSERT INTO tw_minute_archive_release (
                    release_id, release_key, dataset_key, source, upstream_source, overlap_precedence,
                    trading_calendar_checksum, instruments_checksum, trading_dates_checksum,
                    expected_trading_date_count, covered_trading_date_count, chunk_count,
                    coverage_start, coverage_end, instrument_count, row_count, sequence_count,
                    manifest_checksum, content_checksum, manifest, status, finalized_at, created_at
                ) VALUES (
                    :release_id, :release_key, 'tw_equity_minute', 'tw_recorder_archive', 'shioaji',
                    'direct_daily_shioaji', :calendar_checksum, :instruments_checksum,
                    :trading_dates_checksum, 1, 1, 1, '2026-07-01', '2026-07-01',
                    1, 1, 1, :manifest_checksum, :content_checksum, CAST(:manifest AS jsonb),
                    'staged', NULL, now()
                )
            """)
            valid_release_id = uuid4()
            missing_chunks_release_id = uuid4()
            common_release_params = {
                "calendar_checksum": "a" * 64,
                "instruments_checksum": instruments_checksum,
                "trading_dates_checksum": trading_dates_checksum,
                "manifest": json.dumps(manifest),
                "release_key": "tw-2026-07-release",
                "content_checksum": "e" * 64,
            }
            await connection.execute(
                release_insert,
                {
                    **common_release_params,
                    "release_id": valid_release_id,
                    "manifest_checksum": "f" * 64,
                },
            )
            await connection.execute(
                text("""
                    UPDATE tw_minute_archive_release
                    SET manifest_checksum = encode(sha256(convert_to(manifest::text, 'UTF8')), 'hex')
                    WHERE release_id = :release_id
                """),
                {"release_id": valid_release_id},
            )
            await connection.execute(
                release_insert,
                {
                    **common_release_params,
                    "release_id": missing_chunks_release_id,
                    "manifest_checksum": "1" * 64,
                },
            )
            with pytest.raises(DBAPIError, match="invalid finalized archive manifest"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("""
                            UPDATE tw_minute_archive_release
                            SET status = 'finalized', finalized_at = '2026-07-31T00:00:00+00:00'
                            WHERE release_id = :release_id
                        """),
                        {"release_id": missing_chunks_release_id},
                    )

            extra_nested_manifest = json.loads(json.dumps(manifest))
            extra_nested_manifest["release_id"] = "tw-2026-07-extra-nested-key"
            extra_nested_manifest["chunks"][0]["unexpected"] = "forbidden"
            extra_nested_release_id = uuid4()
            await connection.execute(
                release_insert,
                {
                    **common_release_params,
                    "release_id": extra_nested_release_id,
                    "release_key": "tw-2026-07-extra-nested-key",
                    "manifest": json.dumps(extra_nested_manifest),
                    "manifest_checksum": "2" * 64,
                },
            )
            await connection.execute(
                text("""
                    UPDATE tw_minute_archive_release
                    SET manifest_checksum = encode(sha256(convert_to(manifest::text, 'UTF8')), 'hex')
                    WHERE release_id = :release_id
                """),
                {"release_id": extra_nested_release_id},
            )
            with pytest.raises(DBAPIError, match="chunk evidence is malformed"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("""
                            UPDATE tw_minute_archive_release
                            SET status = 'finalized', finalized_at = '2026-07-31T00:00:00+00:00'
                            WHERE release_id = :release_id
                        """),
                        {"release_id": extra_nested_release_id},
                    )

            chunk_id = uuid4()
            await connection.execute(
                text("""
                    INSERT INTO tw_minute_archive_chunk (
                        chunk_id, release_id, snapshot_sequence, chunk_sequence, chunk_count,
                        coverage_start, coverage_end, instrument_count, instrument_checksum,
                        row_count, chunk_checksum, object_key, object_checksum,
                        object_size_bytes, created_at
                    ) VALUES (
                        :chunk_id, :release_id, 1, 1, 1, '2026-07-01', '2026-07-01',
                        1, :instrument_checksum, 1, :chunk_checksum, :object_key,
                        :object_checksum, 42, now()
                    )
                """),
                {
                    "chunk_id": chunk_id,
                    "release_id": valid_release_id,
                    "instrument_checksum": instruments_checksum,
                    "chunk_checksum": chunk_checksum,
                    "object_key": object_key,
                    "object_checksum": object_checksum,
                },
            )
            await connection.execute(
                text("""
                    UPDATE tw_minute_archive_release
                    SET status = 'finalized', finalized_at = '2026-07-31T00:00:00+00:00'
                    WHERE release_id = :release_id
                """),
                {"release_id": valid_release_id},
            )
            with pytest.raises(DBAPIError, match="release is immutable"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("""
                            UPDATE tw_minute_archive_release SET row_count = 2
                            WHERE release_id = :release_id
                        """),
                        {"release_id": valid_release_id},
                    )
            with pytest.raises(DBAPIError, match="chunks are immutable"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("DELETE FROM tw_minute_archive_chunk WHERE chunk_id = :chunk_id"),
                        {"chunk_id": chunk_id},
                    )

            missing_month_manifest = json.loads(json.dumps(manifest))
            missing_month_manifest.update(
                {
                    "release_id": "tw-2026-06-08-gap",
                    "coverage_start_month": "2026-06-01",
                    "coverage_end_month": "2026-08-01",
                    "covered_months": ["2026-06-01", "2026-07-01", "2026-08-01"],
                    "expected_trading_dates": [
                        "2026-06-30",
                        "2026-07-30",
                        "2026-08-31",
                    ],
                    "covered_trading_dates": [
                        "2026-06-30",
                        "2026-07-30",
                        "2026-08-31",
                    ],
                    "trading_dates_sha256": hashlib.sha256(
                        b"2026-06-30\n2026-07-30\n2026-08-31"
                    ).hexdigest(),
                    "chunk_count": 2,
                    "row_count": 2,
                    "checksum_sha256": "5" * 64,
                    "chunks": [
                        {
                            "snapshot_sequence": 1,
                            "chunk_sequence": 1,
                            "chunk_count": 2,
                            "row_count": 1,
                            "checksum_sha256": "1" * 64,
                            "object": {
                                "object_key": "minute/2026-06/chunk-1.parquet",
                                "sha256": "2" * 64,
                                "byte_size": 42,
                            },
                        },
                        {
                            "snapshot_sequence": 1,
                            "chunk_sequence": 2,
                            "chunk_count": 2,
                            "row_count": 1,
                            "checksum_sha256": "3" * 64,
                            "object": {
                                "object_key": "minute/2026-08/chunk-2.parquet",
                                "sha256": "4" * 64,
                                "byte_size": 42,
                            },
                        },
                    ],
                    "objects": [
                        {
                            "object_key": "minute/2026-06/chunk-1.parquet",
                            "sha256": "2" * 64,
                            "byte_size": 42,
                        },
                        {
                            "object_key": "minute/2026-08/chunk-2.parquet",
                            "sha256": "4" * 64,
                            "byte_size": 42,
                        },
                    ],
                }
            )
            gap_release_id = uuid4()
            await connection.execute(
                text("""
                    INSERT INTO tw_minute_archive_release (
                        release_id, release_key, dataset_key, source, upstream_source,
                        overlap_precedence, trading_calendar_checksum, instruments_checksum,
                        trading_dates_checksum, expected_trading_date_count,
                        covered_trading_date_count, chunk_count, coverage_start, coverage_end,
                        instrument_count, row_count, sequence_count, manifest_checksum,
                        content_checksum, manifest, status, finalized_at, created_at
                    ) VALUES (
                        :release_id, 'tw-2026-06-08-gap', 'tw_equity_minute',
                        'tw_recorder_archive', 'shioaji', 'direct_daily_shioaji',
                        :calendar_checksum, :instruments_checksum, :trading_dates_checksum,
                        3, 3, 2, '2026-06-01', '2026-08-01', 1, 2, 1,
                        :manifest_checksum, :content_checksum, CAST(:manifest AS jsonb),
                        'staged', NULL, now()
                    )
                """),
                {
                    "release_id": gap_release_id,
                    "calendar_checksum": "a" * 64,
                    "instruments_checksum": instruments_checksum,
                    "trading_dates_checksum": missing_month_manifest["trading_dates_sha256"],
                    "manifest_checksum": "0" * 64,
                    "content_checksum": "5" * 64,
                    "manifest": json.dumps(missing_month_manifest),
                },
            )
            await connection.execute(
                text("""
                    UPDATE tw_minute_archive_release
                    SET manifest_checksum = encode(sha256(convert_to(manifest::text, 'UTF8')), 'hex')
                    WHERE release_id = :release_id
                """),
                {"release_id": gap_release_id},
            )
            for sequence, month, checksum, stored_object_key, stored_object_checksum in (
                (1, "2026-06-01", "1" * 64, "minute/2026-06/chunk-1.parquet", "2" * 64),
                (2, "2026-08-01", "3" * 64, "minute/2026-08/chunk-2.parquet", "4" * 64),
            ):
                await connection.execute(
                    text("""
                        INSERT INTO tw_minute_archive_chunk (
                            chunk_id, release_id, snapshot_sequence, chunk_sequence, chunk_count,
                            coverage_start, coverage_end, instrument_count, instrument_checksum,
                            row_count, chunk_checksum, object_key, object_checksum,
                            object_size_bytes, created_at
                        ) VALUES (
                            :chunk_id, :release_id, 1, :sequence, 2, CAST(:month AS date),
                            CAST(:month AS date), 1, :instrument_checksum, 1, :chunk_checksum,
                            :object_key, :object_checksum, 42, now()
                        )
                    """),
                    {
                        "chunk_id": uuid4(),
                        "release_id": gap_release_id,
                        "sequence": sequence,
                        "month": date.fromisoformat(month),
                        "instrument_checksum": instruments_checksum,
                        "chunk_checksum": checksum,
                        "object_key": stored_object_key,
                        "object_checksum": stored_object_checksum,
                    },
                )
            with pytest.raises(DBAPIError, match="do not cover every release month"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("""
                            UPDATE tw_minute_archive_release
                            SET status = 'finalized',
                                finalized_at = '2026-07-31T00:00:00+00:00'
                            WHERE release_id = :release_id
                        """),
                        {"release_id": gap_release_id},
                    )

            workflow_release_id = uuid4()
            workflow_etf_release_id = uuid4()
            daily_update_id = uuid4()
            snapshot_id = uuid4()
            etf_snapshot_id = uuid4()
            await connection.execute(
                text("""
                    INSERT INTO tw_minute_universe_release (
                        release_id, dataset_key, effective_date, version, status,
                        member_checksum, member_count, change_count, change_ratio,
                        change_count_threshold, change_ratio_threshold, created_at, updated_at
                    ) VALUES (
                        :release_id, 'tw_equity_minute', '2026-07-30', 1, 'candidate',
                        :member_checksum, 1, 0, 0, 20, 0.02, now(), now()
                    )
                """),
                {"release_id": workflow_release_id, "member_checksum": "9" * 64},
            )
            await connection.execute(
                text("""
                    INSERT INTO tw_minute_universe_release (
                        release_id, dataset_key, effective_date, version, status,
                        member_checksum, member_count, change_count, change_ratio,
                        change_count_threshold, change_ratio_threshold, created_at, updated_at
                    ) VALUES (
                        :release_id, 'tw_etf_minute', '2026-07-30', 1, 'candidate',
                        :member_checksum, 0, 0, 0, 20, 0.02, now(), now()
                    )
                """),
                {"release_id": workflow_etf_release_id, "member_checksum": "5" * 64},
            )
            await connection.execute(
                text("""
                    INSERT INTO tw_minute_daily_update (
                        update_id, environment, market, trade_date, status, created_at, updated_at
                    ) VALUES (:update_id, 'staging', 'TW', '2026-07-30', 'completed', now(), now())
                """),
                {"update_id": daily_update_id},
            )
            await connection.execute(
                text("""
                    INSERT INTO tw_minute_dataset_snapshot (
                        snapshot_id, daily_update_id, dataset_key, trade_date,
                        universe_release_id, symbols_checksum, sequence_count,
                        expected_rows, received_rows, status, symbol_results, created_at, updated_at
                    ) VALUES (
                        :snapshot_id, :update_id, 'tw_equity_minute', '2026-07-30',
                        :release_id, :symbols_checksum, 1, 0, 0, 'completed',
                        '{"2330":{"outcome":"expected_no_data"}}'::jsonb, now(), now()
                    )
                """),
                {
                    "snapshot_id": snapshot_id,
                    "update_id": daily_update_id,
                    "release_id": workflow_release_id,
                    "symbols_checksum": hashlib.sha256(b"2330").hexdigest(),
                },
            )
            await connection.execute(
                text("""
                    INSERT INTO tw_minute_dataset_snapshot (
                        snapshot_id, daily_update_id, dataset_key, trade_date,
                        universe_release_id, symbols_checksum, sequence_count,
                        expected_rows, received_rows, status, symbol_results, created_at, updated_at
                    ) VALUES (
                        :snapshot_id, :update_id, 'tw_etf_minute', '2026-07-30',
                        :release_id, :symbols_checksum, 1, 1, 1, 'completed',
                        '{"0050":{"outcome":"data"}}'::jsonb, now(), now()
                    )
                """),
                {
                    "snapshot_id": etf_snapshot_id,
                    "update_id": daily_update_id,
                    "release_id": workflow_etf_release_id,
                    "symbols_checksum": hashlib.sha256(b"0050").hexdigest(),
                },
            )
            await connection.execute(
                text("""
                    INSERT INTO tw_minute_publication_revision (
                        publication_id, daily_update_id, revision, status, manifest,
                        manifest_checksum, warning_kind, is_latest, published_at, created_at
                    ) VALUES (
                        :publication_id, :update_id, 1, 'completed_with_warnings',
                        '{"expected_no_data_symbols":["2330"]}'::jsonb,
                        :manifest_checksum, 'expected_no_data', false, now(), now()
                    )
                """),
                {
                    "publication_id": uuid4(),
                    "update_id": daily_update_id,
                    "manifest_checksum": "8" * 64,
                },
            )
            with pytest.raises(DBAPIError, match="does not match snapshots"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("""
                            UPDATE tw_minute_dataset_snapshot
                            SET symbol_results = '{"0050":{"outcome":"error"}}'::jsonb
                            WHERE snapshot_id = :snapshot_id
                        """),
                        {"snapshot_id": etf_snapshot_id},
                    )
                    await connection.execute(
                        text("""
                            INSERT INTO tw_minute_publication_revision (
                                publication_id, daily_update_id, revision, status, manifest,
                                manifest_checksum, warning_kind, is_latest, published_at, created_at
                            ) VALUES (
                                :publication_id, :update_id, 2, 'completed_with_warnings',
                                '{"expected_no_data_symbols":["2330"]}'::jsonb,
                                :manifest_checksum, 'expected_no_data', false, now(), now()
                            )
                        """),
                        {
                            "publication_id": uuid4(),
                            "update_id": daily_update_id,
                            "manifest_checksum": "7" * 64,
                        },
                    )
            with pytest.raises(DBAPIError, match="does not match snapshots"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("""
                            INSERT INTO tw_minute_publication_revision (
                                publication_id, daily_update_id, revision, status, manifest,
                                manifest_checksum, warning_kind, is_latest, published_at, created_at
                            ) VALUES (
                                :publication_id, :update_id, 2, 'completed_with_warnings',
                                '{"expected_no_data_symbols":["0050"]}'::jsonb,
                                :manifest_checksum, 'expected_no_data', false, now(), now()
                            )
                        """),
                        {
                            "publication_id": uuid4(),
                            "update_id": daily_update_id,
                            "manifest_checksum": "7" * 64,
                        },
                    )

            instrument_id = uuid4()
            member_id = uuid4()
            shioaji_snapshot = json.dumps(
                {"source": "shioaji", "eligibility": ["2330"]},
                separators=(",", ":"),
                sort_keys=True,
            )
            official_snapshot = json.dumps(
                {
                    "source": "twse",
                    "membership": ["2330"],
                    "classification": {"2330": "equity"},
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            await connection.execute(
                text("""
                    INSERT INTO instruments (
                        instrument_id, asset_class, market, symbol, status, created_at, updated_at
                    ) VALUES (:instrument_id, 'equity', 'TW', '2330', 'active', now(), now())
                """),
                {"instrument_id": instrument_id},
            )
            await connection.execute(
                text("""
                    INSERT INTO tw_minute_universe_member (
                        member_id, release_id, instrument_id, symbol, started_on,
                        source_symbol, created_at, updated_at
                    ) VALUES (
                        :member_id, :release_id, :instrument_id, '2330', '2026-07-30',
                        '2330', now(), now()
                    )
                """),
                {
                    "member_id": member_id,
                    "release_id": workflow_release_id,
                    "instrument_id": instrument_id,
                },
            )
            publish_universe = text("""
                UPDATE tw_minute_universe_release
                SET status = 'published',
                    shioaji_eligibility_snapshot = CAST(:shioaji_snapshot AS jsonb),
                    shioaji_eligibility_checksum = encode(
                        sha256(convert_to(CAST(:shioaji_snapshot AS jsonb)::text, 'UTF8')), 'hex'
                    ),
                    official_membership_snapshot = CAST(:official_snapshot AS jsonb),
                    official_membership_checksum = encode(
                        sha256(convert_to(CAST(:official_snapshot AS jsonb)::text, 'UTF8')), 'hex'
                    ),
                    member_checksum = :member_checksum,
                    change_audit = '{"decision":"published","differences":[]}'::jsonb,
                    published_at = '2026-07-30T06:00:00+00:00'
                WHERE release_id = :release_id
            """)
            publish_parameters = {
                "release_id": workflow_release_id,
                "shioaji_snapshot": shioaji_snapshot,
                "official_snapshot": official_snapshot,
                "member_checksum": "6" * 64,
            }
            with pytest.raises(DBAPIError, match="does not match members or snapshots"):
                async with connection.begin_nested():
                    await connection.execute(publish_universe, publish_parameters)

            actual_member_checksum = await connection.scalar(
                text("""
                    SELECT encode(sha256(convert_to(string_agg(
                        instrument_id::text || '|' || symbol || '|' || started_on::text
                        || '|' || coalesce(ended_on::text, '') || '|'
                        || coalesce(source_symbol, ''),
                        E'\\n' ORDER BY instrument_id::text, symbol, started_on, ended_on, source_symbol
                    ), 'UTF8')), 'hex')
                    FROM tw_minute_universe_member
                    WHERE release_id = :release_id
                """),
                {"release_id": workflow_release_id},
            )
            await connection.execute(
                publish_universe,
                {**publish_parameters, "member_checksum": actual_member_checksum},
            )
            with pytest.raises(DBAPIError, match="release is immutable"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("""
                            UPDATE tw_minute_universe_release
                            SET status = 'candidate'
                            WHERE release_id = :release_id
                        """),
                        {"release_id": workflow_release_id},
                    )
            with pytest.raises(DBAPIError, match="members are immutable"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("""
                            UPDATE tw_minute_universe_member
                            SET symbol = '2330A'
                            WHERE member_id = :member_id
                        """),
                        {"member_id": member_id},
                    )
            await connection.execute(
                text("""
                    UPDATE tw_minute_universe_release
                    SET status = 'superseded'
                    WHERE release_id = :release_id
                """),
                {"release_id": workflow_release_id},
            )
            with pytest.raises(DBAPIError, match="members are immutable"):
                async with connection.begin_nested():
                    await connection.execute(
                        text("""
                            DELETE FROM tw_minute_universe_member
                            WHERE member_id = :member_id
                        """),
                        {"member_id": member_id},
                    )

        await _run_alembic(database_url, "f8a9b0c1d2e3", command="downgrade")
        async with target_engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT to_regclass('public.market_data_minute')"))
                is None
            )

        await _run_alembic(database_url, "a9b0c1d2e3f4")
        async with target_engine.connect() as connection:
            assert await connection.scalar(
                text("SELECT to_regclass('public.tw_minute_archive_chunk')")
            )
    finally:
        if target_engine is not None:
            await target_engine.dispose()
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name"
                ),
                {"name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await admin_engine.dispose()
