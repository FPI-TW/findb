"""Fail production deployment early when the database is unsafe to migrate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DEFAULT_MINIMUM_CONNECTION_HEADROOM = 10


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
    finally:
        await engine.dispose()

    return {
        "alembic_revision": revision,
        "raw_table_size": raw_size,
        "raw_rows": int(raw_rows or 0),
        "duplicate_raw_run_ids": int(duplicate_run_ids or 0),
        "pending_or_processing_with_raw": int(pending_counts.with_raw or 0),
        "pending_or_processing_missing_raw": int(pending_counts.missing_raw or 0),
        "max_connections": max_connections,
        "reserved_connection_slots": reserved_connection_slots,
        "current_connections": current_connections,
        "connection_headroom": calculate_connection_headroom(
            max_connections=max_connections,
            current_connections=current_connections,
            reserved_connection_slots=reserved_connection_slots,
        ),
        "long_transactions_over_5m": long_transactions,
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
    return parser


async def _main() -> int:
    args = _build_parser().parse_args()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("error: DATABASE_URL is required", file=sys.stderr)
        return 2

    state = await collect_predeploy_state(database_url)
    print(json.dumps(state, indent=2, sort_keys=True, default=str))
    errors = validate_predeploy_state(
        state,
        minimum_connection_headroom=max(1, args.minimum_connection_headroom),
    )
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
