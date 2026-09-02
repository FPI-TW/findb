"""Focused coverage for the Wave 5 scheduler projection migration."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.migration_database import get_active_migration_database_factory

BACKEND_ROOT = Path(__file__).resolve().parents[1]
C5_REVISION = "c5d6e7f8a9b0"
D6_REVISION = "d6e7f8a9b0c1"
LATEST_REVISION = "a8b9c0d1e2f3"
HISTORICAL_PROJECTION_CONSTRAINT = "ck_scheduler_control_ck_scheduler_control_dataset_keys_array"
DIRECT_PROJECTION_CONSTRAINT = "ck_scheduler_control_dataset_keys_array"


async def _run_alembic(
    database_url: str,
    revision: str,
    *,
    command: str = "upgrade",
) -> subprocess.CompletedProcess[str]:
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


@asynccontextmanager
async def _c5_database(prefix: str) -> AsyncIterator[tuple[str, AsyncEngine]]:
    """Create a disposable database at the supported pre-Wave-5 revision."""

    async with get_active_migration_database_factory().clone(C5_REVISION, prefix) as database:
        yield database


def test_wave5_is_single_linear_head_and_does_not_edit_c5() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)

    wave5 = scripts.get_revision(D6_REVISION)
    c5 = scripts.get_revision(C5_REVISION)
    assert wave5 is not None
    assert wave5.down_revision == C5_REVISION
    assert c5 is not None
    assert scripts.get_heads() == [LATEST_REVISION]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "payload"),
    (
        ("projection_object", {"not": "an array"}),
        ("projection_non_string", ["us_equity_eod", 1]),
    ),
)
async def test_wave5_projection_shape_fail_closed_without_schema_mutation(
    fixture: str,
    payload: object,
) -> None:
    async with _c5_database(f"findb_wave5_{fixture}") as (database_url, target_engine):
        async with target_engine.begin() as connection:
            await connection.execute(
                text(
                    "ALTER TABLE scheduler_control DROP CONSTRAINT IF EXISTS "
                    "ck_scheduler_control_dataset_keys_array"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE scheduler_control DROP CONSTRAINT IF EXISTS "
                    "ck_scheduler_control_ck_scheduler_control_dataset_keys_array"
                )
            )
            before = await connection.execute(
                text(
                    """
                    SELECT scheduler_key, dataset_keys, slot_id
                    FROM scheduler_control
                    WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                    """
                )
            )
            before_row = before.one()
            await connection.execute(
                text(
                    """
                    UPDATE scheduler_control
                    SET dataset_keys = CAST(:payload AS jsonb)
                    WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                    """
                ),
                {"payload": json.dumps(payload)},
            )

        result = await _run_alembic(database_url, D6_REVISION)
        assert result.returncode != 0
        assert "dataset_keys" in (result.stdout + result.stderr)
        async with target_engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                C5_REVISION
            )
            assert (
                await connection.scalar(
                    text(
                        """
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'scheduler_control'
                      AND column_name = 'dataset_keys'
                    """
                    )
                )
                == 1
            )
            after_row = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, dataset_keys, slot_id
                        FROM scheduler_control
                        WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        """
                    )
                )
            ).one()
            assert after_row == (
                before_row[0],
                payload,
                before_row[2],
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ("association_missing", "association_extra"))
async def test_wave5_association_set_mismatch_fails_closed(
    fixture: str,
) -> None:
    async with _c5_database(f"findb_wave5_{fixture}") as (database_url, target_engine):
        async with target_engine.begin() as connection:
            if fixture == "association_missing":
                await connection.execute(
                    text(
                        """
                        DELETE FROM scheduler_dataset
                        WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        """
                    )
                )
            else:
                await connection.execute(
                    text(
                        """
                        INSERT INTO scheduler_dataset (scheduler_key, dataset_key)
                        VALUES ('twelve_data_us_common_stocks_daily_v1', 'tw_equity_eod')
                        """
                    )
                )

        result = await _run_alembic(database_url, D6_REVISION)
        assert result.returncode != 0
        assert "projection does not match" in (result.stdout + result.stderr)
        async with target_engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                C5_REVISION
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ("slot", "schedule", "legacy_key"))
async def test_wave5_canonical_gates_fail_closed(
    fixture: str,
) -> None:
    async with _c5_database(f"findb_wave5_gate_{fixture}") as (database_url, target_engine):
        async with target_engine.begin() as connection:
            if fixture == "slot":
                await connection.execute(
                    text(
                        """
                        UPDATE scheduler_control
                        SET slot_id = 'legacy_unknown_slot'
                        WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        """
                    )
                )
            elif fixture == "schedule":
                await connection.execute(
                    text(
                        """
                        UPDATE dataset_registry
                        SET config = jsonb_set(
                            config,
                            '{delivery_expectation,schedule}',
                            '"legacy_unknown_slot"'::jsonb,
                            true
                        )
                        WHERE dataset_key = 'us_equity_eod'
                        """
                    )
                )
            else:
                await connection.execute(
                    text(
                        """
                        INSERT INTO scheduler_control (
                            scheduler_key, provider, slot_id, scheduled_local_time,
                            timezone, dataset_keys, desired_state, observed_state,
                            revision, created_at, updated_at
                        ) VALUES (
                            'finlab_tw_1430_tw_equity_eod', 'finlab',
                            'taiwan_market_window', TIME '14:30:00', 'Asia/Taipei',
                            '["tw_equity_eod"]'::jsonb, 'stopped', 'stopped',
                            1, now(), now()
                        )
                        """
                    )
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO scheduler_dataset (scheduler_key, dataset_key)
                        VALUES ('finlab_tw_1430_tw_equity_eod', 'tw_equity_eod')
                        """
                    )
                )

        result = await _run_alembic(database_url, D6_REVISION)
        assert result.returncode != 0
        output = result.stdout + result.stderr
        expected = {
            "slot": "slot_id is not canonical",
            "schedule": "delivery schedule is malformed",
            "legacy_key": "legacy FinLab scheduler key remains",
        }[fixture]
        assert expected in output
        async with target_engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                C5_REVISION
            )


@pytest.mark.asyncio
async def test_wave5_round_trip_rebuilds_sorted_projection() -> None:
    async with _c5_database("findb_wave5_roundtrip") as (database_url, target_engine):
        result = await _run_alembic(database_url, D6_REVISION)
        assert result.returncode == 0, result.stderr
        async with target_engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                D6_REVISION
            )
            assert (
                await connection.scalar(
                    text(
                        """
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'scheduler_control'
                      AND column_name = 'dataset_keys'
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
                    FROM pg_constraint
                    WHERE conrelid = 'scheduler_control'::regclass
                      AND conname = 'ck_scheduler_control_slot_id_canonical'
                    """
                    )
                )
                == 1
            )

        result = await _run_alembic(database_url, C5_REVISION, command="downgrade")
        assert result.returncode == 0, result.stderr
        async with target_engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                C5_REVISION
            )
            projections = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, dataset_keys
                        FROM scheduler_control
                        ORDER BY scheduler_key
                        """
                    )
                )
            ).all()
            assert projections == [
                ("finlab_tw_equity_eod_v1", ["tw_equity_eod"]),
                ("shioaji_tw_pilot_v1", ["tw_equity_minute", "tw_etf_minute"]),
                ("twelve_data_us_common_stocks_daily_v1", ["us_equity_eod"]),
            ]
            assert (
                await connection.scalar(
                    text(
                        """
                    SELECT count(*)
                    FROM pg_constraint
                    WHERE conrelid = 'scheduler_control'::regclass
                      AND conname = 'ck_scheduler_control_slot_id_canonical'
                    """
                    )
                )
                == 0
            )
            constraint_names = set(
                (
                    await connection.execute(
                        text(
                            """
                        SELECT conname
                        FROM pg_constraint
                        WHERE conrelid = 'scheduler_control'::regclass
                          AND contype = 'c'
                        """
                        )
                    )
                ).scalars()
            )
            assert HISTORICAL_PROJECTION_CONSTRAINT in constraint_names
            assert DIRECT_PROJECTION_CONSTRAINT not in constraint_names

        result = await _run_alembic(database_url, D6_REVISION)
        assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_wave5_downgrade_rejects_orphan_association_without_mutation() -> None:
    """A disabled FK must not let downgrade silently lose an association."""

    async with _c5_database("findb_wave5_downgrade_orphan") as (
        database_url,
        target_engine,
    ):
        result = await _run_alembic(database_url, D6_REVISION)
        assert result.returncode == 0, result.stderr

        async with target_engine.begin() as connection:
            await connection.execute(
                text(
                    "ALTER TABLE scheduler_dataset DROP CONSTRAINT "
                    "fk_scheduler_dataset_scheduler_key"
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO scheduler_dataset (scheduler_key, dataset_key)
                    VALUES ('orphan_scheduler_for_wave5', 'us_equity_eod')
                    """
                )
            )
            before_controls = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, slot_id, scheduled_local_time,
                               timezone, revision
                        FROM scheduler_control
                        ORDER BY scheduler_key
                        """
                    )
                )
            ).all()
            before_associations = (
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

        result = await _run_alembic(database_url, C5_REVISION, command="downgrade")
        assert result.returncode != 0
        assert "orphan scheduler_dataset association" in (result.stdout + result.stderr)

        async with target_engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                D6_REVISION
            )
            assert (
                await connection.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'scheduler_control'
                          AND column_name = 'dataset_keys'
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
                        FROM pg_constraint
                        WHERE conrelid = 'scheduler_control'::regclass
                          AND conname = 'ck_scheduler_control_slot_id_canonical'
                        """
                    )
                )
                == 1
            )
            after_controls = (
                await connection.execute(
                    text(
                        """
                        SELECT scheduler_key, slot_id, scheduled_local_time,
                               timezone, revision
                        FROM scheduler_control
                        ORDER BY scheduler_key
                        """
                    )
                )
            ).all()
            after_associations = (
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
            assert after_controls == before_controls
            assert after_associations == before_associations
