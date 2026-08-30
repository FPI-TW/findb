"""Fail production deployment early when the database is unsafe to migrate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any
from urllib.parse import parse_qs, urlsplit

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.slot_identity import CANONICAL_SLOT_IDS

DEFAULT_MINIMUM_CONNECTION_HEADROOM = 10
_CANONICAL_SLOT_SQL = ", ".join(f"'{slot}'" for slot in CANONICAL_SLOT_IDS)


def database_url_requires_tls(database_url: str) -> bool:
    """Return whether a PostgreSQL URL has an explicit non-disabled TLS mode."""
    query = parse_qs(urlsplit(database_url).query)
    sslmode = query.get("sslmode", query.get("ssl", [""]))[0].lower()
    return sslmode in {"require", "verify-ca", "verify-full"}


def database_url_matches_expected_host(database_url: str, expected_host: str) -> bool:
    """Bind the loaded runtime URL to the reviewed RDS endpoint without logging it."""
    hostname = urlsplit(database_url).hostname
    return bool(hostname) and hostname.casefold() == expected_host.strip().casefold()


def database_revision_matches_expected(
    *, current_revision: str | None, expected_revision: str
) -> bool:
    """Return whether the database is already at the selected exact revision."""
    return bool(current_revision) and current_revision == expected_revision


def is_schema_compatible_with_target(
    *, current_revision: str | None, target_revision: str
) -> bool:
    """Require the running schema to be an ancestor of the selected application.

    A rollback image whose migration graph cannot reach the current schema is
    rejected before writers are stopped.  Alembic's script directory is the
    application-owned compatibility authority; do not infer ordering from IDs.
    """
    if not current_revision:
        return False
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    try:
        return any(
            revision.revision == current_revision
            for revision in scripts.iterate_revisions(target_revision, "base")
        )
    except Exception:
        return False


def calculate_connection_headroom(
    *,
    max_connections: int,
    current_connections: int,
    reserved_connection_slots: int,
) -> int:
    """Return slots available to ordinary client connections."""
    return max(0, max_connections - reserved_connection_slots - current_connections)


async def collect_predeploy_state(database_url: str) -> dict[str, Any]:
    """Collect migration risk indicators without changing database state."""
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            raw_size = await connection.scalar(
                text("SELECT pg_size_pretty(pg_total_relation_size('raw.market_payload'))")
            )
            raw_rows = await connection.scalar(text("SELECT count(*) FROM raw.market_payload"))
            duplicate_run_ids = await connection.scalar(
                text("""
                    SELECT count(*)
                    FROM (
                        SELECT run_id
                        FROM raw.market_payload
                        WHERE run_id IS NOT NULL
                        GROUP BY run_id
                        HAVING count(*) > 1
                    ) AS duplicates
                    """)
            )
            pending_counts = (
                await connection.execute(
                    text("""
                        SELECT
                            count(*) FILTER (WHERE raw.run_id IS NOT NULL) AS with_raw,
                            count(*) FILTER (WHERE raw.run_id IS NULL) AS missing_raw
                        FROM ingestion_run AS run
                        LEFT JOIN raw.market_payload AS raw ON raw.run_id = run.run_id
                        WHERE run.status IN ('pending', 'processing')
                        """)
                )
            ).one()
            noncanonical_scheduler_slots = int(
                await connection.scalar(
                    text(
                        f"""
                        SELECT count(*)
                        FROM scheduler_control
                        WHERE slot_id IS NULL
                           OR slot_id NOT IN ({_CANONICAL_SLOT_SQL})
                        """
                    )
                )
                or 0
            )
            noncanonical_delivery_schedule_slots = int(
                await connection.scalar(
                    text(
                        f"""
                        SELECT count(*)
                        FROM dataset_registry
                        WHERE config->'delivery_expectation'->'schedule' IS NOT NULL
                          AND jsonb_typeof(
                                config->'delivery_expectation'->'schedule'
                              ) <> 'null'
                          AND (
                                jsonb_typeof(
                                    config->'delivery_expectation'->'schedule'
                                ) <> 'object'
                                OR COALESCE(
                                    config#>>'{{delivery_expectation,schedule,slot_id}}', ''
                                ) NOT IN ({_CANONICAL_SLOT_SQL})
                          )
                        """
                    )
                )
                or 0
            )
            legacy_finlab_scheduler_keys = int(
                await connection.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM scheduler_control
                        WHERE scheduler_key = 'finlab_tw_1430_tw_equity_eod'
                        """
                    )
                )
                or 0
            )
            # Wave 5 removes the projection column.  Keep this preflight
            # compatible with both the c5 expand schema and the d6 contract
            # schema: a missing projection is expected and represented by a
            # zero mismatch count plus an explicit N/A status.
            dataset_keys_projection_present = bool(
                await connection.scalar(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND table_name = 'scheduler_control'
                              AND column_name = 'dataset_keys'
                        )
                        """
                    )
                )
            )
            if dataset_keys_projection_present:
                dataset_keys_projection_mismatches = int(
                    await connection.scalar(
                        text(
                            """
                            SELECT count(*)
                            FROM scheduler_control AS control
                            WHERE control.dataset_keys IS NULL
                               OR jsonb_typeof(control.dataset_keys) <> 'array'
                               OR EXISTS (
                                    SELECT 1
                                    FROM jsonb_array_elements(
                                        CASE
                                            WHEN jsonb_typeof(control.dataset_keys) = 'array'
                                            THEN control.dataset_keys
                                            ELSE '[]'::jsonb
                                        END
                                    ) AS item(value)
                                    WHERE jsonb_typeof(item.value) <> 'string'
                               )
                               OR EXISTS (
                                    SELECT projected.dataset_key
                                    FROM jsonb_array_elements_text(
                                        CASE
                                            WHEN jsonb_typeof(control.dataset_keys) = 'array'
                                            THEN control.dataset_keys
                                            ELSE '[]'::jsonb
                                        END
                                    ) AS projected(dataset_key)
                                    EXCEPT
                                    SELECT association.dataset_key
                                    FROM scheduler_dataset AS association
                                    WHERE association.scheduler_key = control.scheduler_key
                               )
                               OR EXISTS (
                                    SELECT association.dataset_key
                                    FROM scheduler_dataset AS association
                                    WHERE association.scheduler_key = control.scheduler_key
                                    EXCEPT
                                    SELECT projected.dataset_key
                                    FROM jsonb_array_elements_text(
                                        CASE
                                            WHEN jsonb_typeof(control.dataset_keys) = 'array'
                                            THEN control.dataset_keys
                                            ELSE '[]'::jsonb
                                        END
                                    ) AS projected(dataset_key)
                               )
                            """
                        )
                    )
                    or 0
                )
            else:
                dataset_keys_projection_mismatches = 0
            max_connections = int(
                await connection.scalar(text("SELECT current_setting('max_connections')::int")) or 0
            )
            reserved_connection_slots = int(
                await connection.scalar(
                    text("""
                        SELECT
                            current_setting('superuser_reserved_connections')::int
                            + COALESCE(
                                NULLIF(current_setting('reserved_connections', true), '')::int,
                                0
                            )
                        """)
                )
                or 0
            )
            current_connections = int(
                await connection.scalar(
                    text("""
                        SELECT count(*)
                        FROM pg_stat_activity
                        WHERE backend_type = 'client backend'
                        """)
                )
                or 0
            )
            long_transactions = int(
                await connection.scalar(
                    text("""
                        SELECT count(*)
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND pid <> pg_backend_pid()
                          AND xact_start IS NOT NULL
                          AND now() - xact_start > interval '5 minutes'
                        """)
                )
                or 0
            )
            tls_in_use = bool(
                await connection.scalar(
                    text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
                )
            )
    finally:
        await engine.dispose()

    return {
        "alembic_revision": revision,
        "raw_table_size": raw_size,
        "raw_rows": int(raw_rows or 0),
        "duplicate_raw_run_ids": int(duplicate_run_ids or 0),
        "pending_or_processing_with_raw": int(pending_counts.with_raw or 0),
        "pending_or_processing_missing_raw": int(pending_counts.missing_raw or 0),
        "noncanonical_scheduler_control_slots": noncanonical_scheduler_slots,
        "noncanonical_dataset_delivery_schedule_slots": noncanonical_delivery_schedule_slots,
        "legacy_finlab_scheduler_keys": legacy_finlab_scheduler_keys,
        "dataset_keys_projection_mismatches": dataset_keys_projection_mismatches,
        "dataset_keys_projection_status": (
            "present" if dataset_keys_projection_present else "not_applicable"
        ),
        "max_connections": max_connections,
        "reserved_connection_slots": reserved_connection_slots,
        "current_connections": current_connections,
        "connection_headroom": calculate_connection_headroom(
            max_connections=max_connections,
            current_connections=current_connections,
            reserved_connection_slots=reserved_connection_slots,
        ),
        "long_transactions_over_5m": long_transactions,
        "postgresql_tls_in_use": tls_in_use,
    }


def validate_predeploy_state(
    state: dict[str, Any],
    *,
    minimum_connection_headroom: int,
) -> list[str]:
    """Return deployment blockers found in a collected database snapshot."""
    errors: list[str] = []
    if state["duplicate_raw_run_ids"]:
        errors.append("raw.market_payload contains duplicate non-null run_id values")
    if state["long_transactions_over_5m"]:
        errors.append("database has transactions older than five minutes")
    if state["connection_headroom"] < minimum_connection_headroom:
        errors.append(
            f"database connection headroom is below {minimum_connection_headroom} connections"
        )
    if state.get("postgresql_tls_in_use") is not True:
        errors.append("database connection did not negotiate PostgreSQL TLS")
    if state.get("noncanonical_scheduler_control_slots", 0):
        errors.append("scheduler_control contains non-canonical slot_id values")
    if state.get("noncanonical_dataset_delivery_schedule_slots", 0):
        errors.append("dataset delivery schedules contain non-canonical slot_id values")
    if state.get("legacy_finlab_scheduler_keys", 0):
        errors.append("legacy FinLab scheduler keys are still present")
    if state.get("dataset_keys_projection_mismatches", 0):
        errors.append("dataset_keys projection does not match scheduler_dataset associations")
    return errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--minimum-connection-headroom",
        type=int,
        default=int(
            os.getenv(
                "PREDEPLOY_MIN_DB_CONNECTION_HEADROOM",
                str(DEFAULT_MINIMUM_CONNECTION_HEADROOM),
            )
        ),
        help=(
            "Required free ordinary database connection slots "
            f"(default: {DEFAULT_MINIMUM_CONNECTION_HEADROOM})"
        ),
    )
    parser.add_argument(
        "--expected-rds-endpoint",
        help="Expected RDS Endpoint.Address for the host-loaded staging database URL.",
    )
    parser.add_argument(
        "--expected-alembic-revision",
        help=(
            "Selected image Alembic head. When supplied, fail closed unless the "
            "current database revision is an ancestor in that image's migration graph."
        ),
    )
    parser.add_argument(
        "--require-exact-alembic-revision",
        action="store_true",
        help=(
            "Require the database to already be exactly at "
            "--expected-alembic-revision; activation never performs a migration."
        ),
    )
    return parser


async def _main() -> int:
    args = _build_parser().parse_args()
    if args.require_exact_alembic_revision and not args.expected_alembic_revision:
        print(
            "error: --require-exact-alembic-revision needs --expected-alembic-revision",
            file=sys.stderr,
        )
        return 2
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("error: DATABASE_URL is required", file=sys.stderr)
        return 2

    if not database_url_requires_tls(database_url):
        print("error: DATABASE_URL must require PostgreSQL TLS", file=sys.stderr)
        return 1
    if args.expected_rds_endpoint and not database_url_matches_expected_host(
        database_url, args.expected_rds_endpoint
    ):
        print("error: DATABASE_URL host does not match the expected RDS endpoint", file=sys.stderr)
        return 1

    state = await collect_predeploy_state(database_url)
    if args.expected_alembic_revision:
        state["target_alembic_revision"] = args.expected_alembic_revision
        state["schema_compatible_with_target"] = is_schema_compatible_with_target(
            current_revision=state["alembic_revision"],
            target_revision=args.expected_alembic_revision,
        )
        if args.require_exact_alembic_revision:
            state["schema_exactly_at_target"] = database_revision_matches_expected(
                current_revision=state["alembic_revision"],
                expected_revision=args.expected_alembic_revision,
            )
    print(json.dumps(state, indent=2, sort_keys=True, default=str))
    errors = validate_predeploy_state(
        state,
        minimum_connection_headroom=max(1, args.minimum_connection_headroom),
    )
    if args.expected_alembic_revision and not state["schema_compatible_with_target"]:
        errors.append("current database revision is not compatible with the selected target")
    if args.require_exact_alembic_revision and not state["schema_exactly_at_target"]:
        errors.append("database revision is not exactly the selected target")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
