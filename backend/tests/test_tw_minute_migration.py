import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import date
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
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", command, revision],
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
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
    assert foundation is not None
    assert foundation.down_revision == "f8a9b0c1d2e3"
    assert activation is not None
    assert activation.down_revision == "a9b0c1d2e3f4"
    assert scripts.get_heads() == ["b0c1d2e3f4a5"]


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
