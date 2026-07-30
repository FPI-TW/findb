"""Reset all mutable PostgreSQL data in the FinDB staging environment.

The reset deliberately preserves only schema/configuration/authentication data:

* ``public.alembic_version``
* ``public.dataset_registry``
* ``public.source_client``
* ``public.api_key``
* ``public.admin_user``
* ``public.admin_session``
* ``public.admin_audit_event``
* ``public.calendar_market``
* ``public.credential_usage_rollup``

Every other current application data table is cleared, including canonical,
raw, workflow, audit, alert, calendar, roll-rule, and worker-heartbeat data.

The default mode is a read-only dry run. Applying the reset requires both an
exact staging confirmation and an assertion that every database writer
(Fetcher scheduler, ingest, dispatcher, worker, and raw-cleanup) has already
been stopped by the operator. This script does not stop remote services and
does not touch R2 or Fetcher checkpoints.

Safe two-step workflow:

1. Run this script without arguments and review its counts and
   ``target_fingerprint``.
2. Stop all staging database writers, then rerun with ``--apply``,
   ``--confirm-staging RESET-STAGING-MUTABLE-DATA``,
   ``--confirm-target-fingerprint <dry-run fingerprint>``, and
   ``--confirm-db-writers-stopped``.

The fingerprint is a SHA-256 digest of stable live connection identity fields;
the underlying database/user/server identity and all credentials remain hidden.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.config import get_settings

STAGING_CONFIRMATION = "RESET-STAGING-MUTABLE-DATA"
ADVISORY_LOCK_KEY = 7_135_202_607_280_001
TERMINAL_STATUSES = ("completed", "completed_with_errors", "failed")

PROTECTED_TABLES = (
    "public.admin_audit_event",
    "public.admin_session",
    "public.admin_user",
    "public.alembic_version",
    "public.api_key",
    "public.calendar_market",
    "public.credential_usage_rollup",
    "public.dataset_registry",
    "public.source_client",
)

# This list is intentionally explicit. A schema-coverage test fails when a new
# application model is added without a deliberate preserve-or-reset decision.
TARGET_TABLES = (
    "public.bond_details",
    "public.bond_eod",
    "public.calendar_import_batch",
    "public.calendar_revision_day",
    "public.calendar_year_revision",
    "public.canonical_correction",
    "public.corporate_action",
    "public.dq_issue",
    "public.etf_details",
    "public.futures_continuous_eod",
    "public.futures_contract",
    "public.ingestion_attempt",
    "public.ingestion_run",
    "public.instrument_identifiers",
    "public.instrument_stats",
    "public.instruments",
    "public.macro_observation",
    "public.macro_series",
    "public.market_data_eod",
    "public.missing_delivery_alert",
    "public.normalization_job",
    "public.normalization_outbox",
    "public.normalization_worker_heartbeat",
    "public.roll_rule",
    "public.trading_calendar",
    "raw.market_payload",
)


class ResetSafetyError(RuntimeError):
    """Raised when a staging reset safety precondition is not satisfied."""


@dataclass(frozen=True)
class ResetReport:
    mode: str
    target_fingerprint: str
    before: Mapping[str, int]
    after: Mapping[str, int]


async def _scalar_count(
    connection: AsyncConnection, statement: str, params: Mapping[str, object] | None = None
) -> int:
    value = await connection.scalar(text(statement), params or {})
    return int(value or 0)


async def _acquire_lock(connection: AsyncConnection) -> None:
    acquired = await connection.scalar(
        text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
        {"lock_key": ADVISORY_LOCK_KEY},
    )
    if acquired is not True:
        raise ResetSafetyError("another staging reset transaction is already running")


async def get_target_fingerprint(connection: AsyncConnection) -> str:
    """Return a non-secret digest identifying the live database target."""
    row = (
        await connection.execute(
            text("""
                SELECT current_database(),
                       current_user,
                       COALESCE(inet_server_addr()::text, 'local-socket'),
                       COALESCE(inet_server_port()::text, 'local-socket')
            """)
        )
    ).one()
    identity = "\x00".join(str(value) for value in row)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


async def _assert_inactive(connection: AsyncConnection) -> None:
    params = {"terminal_statuses": list(TERMINAL_STATUSES)}
    active_runs = await _scalar_count(
        connection,
        """
        SELECT count(*) FROM ingestion_run
         WHERE status <> ALL(CAST(:terminal_statuses AS text[]))
        """,
        params,
    )
    active_jobs = await _scalar_count(
        connection,
        """
        SELECT count(*) FROM normalization_job
         WHERE status <> ALL(CAST(:terminal_statuses AS text[]))
        """,
        params,
    )
    if active_runs or active_jobs:
        raise ResetSafetyError(
            "staging reset refused: nonterminal work exists "
            f"(runs={active_runs}, jobs={active_jobs})"
        )


async def _table_exists(connection: AsyncConnection, qualified_name: str) -> bool:
    exists = await connection.scalar(
        text("SELECT to_regclass(:qualified_name) IS NOT NULL"),
        {"qualified_name": qualified_name},
    )
    return exists is True


async def _snapshot(connection: AsyncConnection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table_name in sorted((*PROTECTED_TABLES, *TARGET_TABLES)):
        # Base.metadata.create_all-based tests do not create alembic_version.
        # Production/staging is expected to have it and its absence is still
        # visible as -1 without making dry-run unusable in isolated tests.
        counts[table_name] = (
            await _scalar_count(connection, f"SELECT count(*) FROM {table_name}")
            if await _table_exists(connection, table_name)
            else -1
        )
    return counts


async def _truncate_mutable_rows(connection: AsyncConnection) -> None:
    # All mutable tables are named explicitly and CASCADE is intentionally
    # forbidden. PostgreSQL will fail closed if an unlisted FK dependency is
    # introduced. One transactional TRUNCATE avoids millions of row-level
    # DELETE operations and handles the raw/run and DQ/run relationships.
    table_list = ", ".join(TARGET_TABLES)
    await connection.execute(text(f"TRUNCATE TABLE {table_list} RESTART IDENTITY"))


async def reset_staging_legacy_data(
    connection: AsyncConnection,
    *,
    apply: bool,
    db_writers_stopped: bool = False,
    target_fingerprint: str | None = None,
) -> ResetReport:
    """Inspect or apply the staging reset inside the caller's transaction."""
    if apply and not db_writers_stopped:
        raise ResetSafetyError(
            "apply requires an explicit assertion that all staging database writers are stopped"
        )

    await _acquire_lock(connection)
    live_fingerprint = await get_target_fingerprint(connection)
    if apply and target_fingerprint != live_fingerprint:
        raise ResetSafetyError(
            "apply requires --confirm-target-fingerprint to exactly match "
            "the fingerprint from a fresh dry run"
        )
    await _assert_inactive(connection)
    before = await _snapshot(connection)
    if not apply:
        return ResetReport(
            mode="dry-run",
            target_fingerprint=live_fingerprint,
            before=before,
            after=before,
        )

    await _truncate_mutable_rows(connection)
    after = await _snapshot(connection)
    remaining = {table: after[table] for table in TARGET_TABLES if after[table] != 0}
    if remaining:
        detail = ", ".join(f"{table}={value}" for table, value in sorted(remaining.items()))
        raise ResetSafetyError(f"mutable rows remain after reset: {detail}")
    for table_name in PROTECTED_TABLES:
        if before[table_name] != after[table_name]:
            raise ResetSafetyError(
                f"protected table count changed: {table_name} "
                f"({before[table_name]} -> {after[table_name]})"
            )
    return ResetReport(
        mode="apply",
        target_fingerprint=live_fingerprint,
        before=before,
        after=after,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the staging-only database reset. Default is dry-run.",
    )
    parser.add_argument(
        "--confirm-staging",
        metavar="PHRASE",
        help=f"Required with --apply; exact value: {STAGING_CONFIRMATION}",
    )
    parser.add_argument(
        "--confirm-target-fingerprint",
        metavar="SHA256",
        help="Required with --apply; copy target_fingerprint from a fresh dry run.",
    )
    parser.add_argument(
        "--confirm-db-writers-stopped",
        action="store_true",
        help=(
            "Assert Fetcher scheduler, ingest, dispatcher, worker, raw-cleanup, "
            "and all other database writers have already been stopped."
        ),
    )
    return parser.parse_args()


def validate_cli_safety(args: argparse.Namespace) -> None:
    if not args.apply:
        return
    if args.confirm_staging != STAGING_CONFIRMATION:
        raise ResetSafetyError(f"--apply requires --confirm-staging {STAGING_CONFIRMATION}")
    if not args.confirm_target_fingerprint:
        raise ResetSafetyError("--apply requires --confirm-target-fingerprint from a fresh dry run")
    if not args.confirm_db_writers_stopped:
        raise ResetSafetyError("--apply requires --confirm-db-writers-stopped")


def _print_report(report: ResetReport) -> None:
    print(f"mode={report.mode}")
    print(f"target_fingerprint={report.target_fingerprint}")
    print("scope=postgresql-only; r2=untouched; fetcher_checkpoints=untouched")
    for phase, counts in (("before", report.before), ("after", report.after)):
        for label, value in sorted(counts.items()):
            print(f"{phase}.{label}={value}")


async def _main(args: argparse.Namespace) -> None:
    validate_cli_safety(args)
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    try:
        async with engine.begin() as connection:
            report = await reset_staging_legacy_data(
                connection,
                apply=args.apply,
                db_writers_stopped=args.confirm_db_writers_stopped,
                target_fingerprint=args.confirm_target_fingerprint,
            )
        _print_report(report)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(_main(_parse_args()))
    except ResetSafetyError as exc:
        raise SystemExit(f"refused: {exc}") from exc
