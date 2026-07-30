"""Fetcher-owned SQLite state for schedules, retries, leases, and checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from findb_fetcher.schedule import ScheduleConfig, ScheduleError
from findb_fetcher.universe import SymbolUniverse

_SCHEMA_VERSION = 2
_EXPECTED_COLUMNS = {
    "scheduled_job": {
        "job_key": ("TEXT", 0, 1),
        "slot_id": ("TEXT", 1, 0),
        "provider": ("TEXT", 1, 0),
        "dataset_key": ("TEXT", 1, 0),
        "work_item": ("TEXT", 1, 0),
        "target_data_date": ("TEXT", 1, 0),
        "schedule_id": ("TEXT", 1, 0),
        "scheduled_date": ("TEXT", 1, 0),
        "universe_id": ("TEXT", 1, 0),
        "universe_version": ("INTEGER", 1, 0),
        "symbol": ("TEXT", 1, 0),
        "canonical_symbol": ("TEXT", 1, 0),
        "exchange": ("TEXT", 1, 0),
        "status": ("TEXT", 1, 0),
        "attempt_count": ("INTEGER", 1, 0),
        "next_attempt_at": ("TEXT", 0, 0),
        "lease_until": ("TEXT", 0, 0),
        "last_outcome": ("TEXT", 0, 0),
        "record_count": ("INTEGER", 0, 0),
        "checkpoint_before": ("TEXT", 0, 0),
        "checkpoint_after": ("TEXT", 0, 0),
        "attempt_id": ("TEXT", 0, 0),
        "run_id": ("TEXT", 0, 0),
        "request_key": ("TEXT", 0, 0),
        "idempotency_key": ("TEXT", 0, 0),
        "prepared_request": ("TEXT", 0, 0),
        "created_at": ("TEXT", 1, 0),
        "updated_at": ("TEXT", 1, 0),
        "completed_at": ("TEXT", 0, 0),
    },
    "symbol_checkpoint": {
        "provider": ("TEXT", 1, 1),
        "dataset_key": ("TEXT", 1, 2),
        "universe_id": ("TEXT", 1, 3),
        "universe_version": ("INTEGER", 1, 4),
        "symbol": ("TEXT", 1, 5),
        "trade_date": ("TEXT", 1, 0),
        "updated_at": ("TEXT", 1, 0),
    },
}


class SchedulerStateError(RuntimeError):
    """The local durable scheduler state cannot be used safely."""


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    job_key: str
    schedule_id: str
    scheduled_date: date
    universe_id: str
    universe_version: int
    symbol: str
    canonical_symbol: str
    exchange: str
    status: str
    attempt_count: int
    checkpoint_before: date | None
    prepared_request: dict[str, Any] | None
    slot_id: str = "legacy"
    provider: str = "twelve_data"
    dataset_key: str = "us_equity_eod"
    work_item: str = ""
    target_data_date: date | None = None


class SchedulerState:
    """Small transactional state store; it never connects to the FinDB database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise SchedulerStateError(
                f"unable to create scheduler state directory: {path.parent}"
            ) from exc
        self._initialize()

    def enqueue_due(
        self,
        schedule: ScheduleConfig,
        universe: SymbolUniverse,
        scheduled_date: date,
        *,
        target_data_date: date | None = None,
        now: datetime,
    ) -> int:
        timestamp = _datetime_text(now)
        target_date = target_data_date or scheduled_date
        inserted = 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if schedule.schedule_version == 2:
                connection.execute(
                    """
                    UPDATE scheduled_job
                    SET slot_id = ?,
                        schedule_id = ?,
                        work_item = symbol,
                        target_data_date = scheduled_date,
                        scheduled_date = CASE
                            WHEN ? = 'us_0600' THEN date(scheduled_date, '+1 day')
                            ELSE scheduled_date
                        END,
                        updated_at = ?
                    WHERE slot_id = 'legacy'
                      AND provider = ?
                      AND dataset_key = ?
                      AND universe_id = ?
                      AND universe_version = ?
                    """,
                    (
                        schedule.slot_id,
                        schedule.schedule_id,
                        schedule.slot_id,
                        timestamp,
                        schedule.provider,
                        schedule.dataset_key,
                        universe.universe_id,
                        universe.universe_version,
                    ),
                )
            for member in universe.symbols:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO scheduled_job (
                        job_key, slot_id, provider, dataset_key, work_item, target_data_date, schedule_id, scheduled_date, universe_id,
                        universe_version, symbol, canonical_symbol, exchange,
                        status, attempt_count, next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (
                        _job_key(schedule, scheduled_date, member.symbol),
                        schedule.slot_id,
                        schedule.provider,
                        schedule.dataset_key,
                        member.symbol,
                        target_date.isoformat(),
                        schedule.schedule_id,
                        scheduled_date.isoformat(),
                        universe.universe_id,
                        universe.universe_version,
                        member.symbol,
                        member.canonical_symbol,
                        member.exchange,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                inserted += cursor.rowcount
            connection.commit()
        return inserted

    def claim_due(
        self,
        schedule: ScheduleConfig,
        universe: SymbolUniverse,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[ScheduledJob, ...]:
        if limit < 1 or limit > len(universe.symbols):
            raise ScheduleError("claim limit must fit within the governed universe")
        now_text = _datetime_text(now)
        lease_until = _datetime_text(now + timedelta(seconds=schedule.lease_seconds))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE scheduled_job
                SET status = 'retry_wait',
                    next_attempt_at = ?,
                    lease_until = NULL,
                    last_outcome = 'lease_expired',
                    updated_at = ?
                WHERE schedule_id = ?
                  AND universe_id = ?
                  AND universe_version = ?
                  AND status = 'running'
                  AND lease_until <= ?
                """,
                (
                    now_text,
                    now_text,
                    schedule.schedule_id,
                    universe.universe_id,
                    universe.universe_version,
                    now_text,
                ),
            )
            rows = connection.execute(
                """
                SELECT job_key, slot_id, provider, dataset_key, work_item, target_data_date, schedule_id, scheduled_date, universe_id,
                       universe_version, symbol, canonical_symbol, exchange,
                       status, attempt_count, prepared_request
                FROM scheduled_job AS candidate
                WHERE candidate.schedule_id = ?
                  AND candidate.universe_id = ?
                  AND candidate.universe_version = ?
                  AND candidate.status IN ('pending', 'retry_wait')
                  AND candidate.next_attempt_at <= ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM scheduled_job AS earlier
                      WHERE earlier.schedule_id = candidate.schedule_id
                        AND earlier.symbol = candidate.symbol
                        AND earlier.scheduled_date < candidate.scheduled_date
                        AND earlier.status IN ('pending', 'running', 'retry_wait')
                  )
                ORDER BY candidate.scheduled_date, candidate.symbol
                LIMIT ?
                """,
                (
                    schedule.schedule_id,
                    universe.universe_id,
                    universe.universe_version,
                    now_text,
                    limit,
                ),
            ).fetchall()
            jobs: list[ScheduledJob] = []
            for row in rows:
                connection.execute(
                    """
                    UPDATE scheduled_job
                    SET status = 'running',
                        attempt_count = attempt_count + 1,
                        lease_until = ?,
                        updated_at = ?
                    WHERE job_key = ?
                    """,
                    (lease_until, now_text, row["job_key"]),
                )
                checkpoint = connection.execute(
                    """
                    SELECT trade_date
                    FROM symbol_checkpoint
                    WHERE provider = ? AND dataset_key = ? AND universe_id = ? AND universe_version = ? AND symbol = ?
                    """,
                    (
                        row["provider"],
                        row["dataset_key"],
                        row["universe_id"],
                        row["universe_version"],
                        row["symbol"],
                    ),
                ).fetchone()
                jobs.append(
                    ScheduledJob(
                        job_key=row["job_key"],
                        schedule_id=row["schedule_id"],
                        scheduled_date=date.fromisoformat(row["scheduled_date"]),
                        universe_id=row["universe_id"],
                        universe_version=row["universe_version"],
                        symbol=row["symbol"],
                        canonical_symbol=row["canonical_symbol"],
                        exchange=row["exchange"],
                        status="running",
                        attempt_count=row["attempt_count"] + 1,
                        checkpoint_before=(
                            date.fromisoformat(checkpoint["trade_date"]) if checkpoint else None
                        ),
                        prepared_request=_optional_request(row["prepared_request"]),
                        slot_id=row["slot_id"],
                        provider=row["provider"],
                        dataset_key=row["dataset_key"],
                        work_item=row["work_item"],
                        target_data_date=date.fromisoformat(row["target_data_date"]),
                    )
                )
            connection.commit()
        return tuple(jobs)

    def save_prepared_request(
        self,
        job: ScheduledJob,
        request: dict[str, Any],
        *,
        now: datetime,
    ) -> None:
        try:
            encoded = json.dumps(
                request,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise SchedulerStateError("prepared request must be deterministic JSON") from exc
        if len(encoded.encode()) > 2 * 1024 * 1024:
            raise SchedulerStateError("prepared request exceeds scheduler state limit")
        timestamp = _datetime_text(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE scheduled_job
                SET prepared_request = ?,
                    request_key = ?,
                    idempotency_key = ?,
                    updated_at = ?
                WHERE job_key = ? AND status = 'running'
                """,
                (
                    encoded,
                    request.get("request_key"),
                    request.get("idempotency_key"),
                    timestamp,
                    job.job_key,
                ),
            ).rowcount
            if updated != 1:
                raise SchedulerStateError("scheduled job is no longer running")
            connection.commit()

    def complete(
        self,
        job: ScheduledJob,
        *,
        now: datetime,
        outcome: str,
        record_count: int,
        checkpoint_after: date | None,
        attempt_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> None:
        timestamp = _datetime_text(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if checkpoint_after is not None:
                connection.execute(
                    """
                    INSERT INTO symbol_checkpoint (
                        provider, dataset_key, universe_id, universe_version, symbol, trade_date, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (provider, dataset_key, universe_id, universe_version, symbol)
                    DO UPDATE SET trade_date = excluded.trade_date, updated_at = excluded.updated_at
                    WHERE excluded.trade_date > symbol_checkpoint.trade_date
                    """,
                    (
                        job.provider,
                        job.dataset_key,
                        job.universe_id,
                        job.universe_version,
                        job.symbol,
                        checkpoint_after.isoformat(),
                        timestamp,
                    ),
                )
            updated = connection.execute(
                """
                UPDATE scheduled_job
                SET status = 'completed',
                    lease_until = NULL,
                    next_attempt_at = NULL,
                    last_outcome = ?,
                    record_count = ?,
                    checkpoint_before = ?,
                    checkpoint_after = ?,
                    attempt_id = ?,
                    run_id = ?,
                    prepared_request = NULL,
                    updated_at = ?,
                    completed_at = ?
                WHERE job_key = ? AND status = 'running'
                """,
                (
                    outcome,
                    record_count,
                    _optional_date_text(job.checkpoint_before),
                    _optional_date_text(checkpoint_after),
                    str(attempt_id) if attempt_id else None,
                    str(run_id) if run_id else None,
                    timestamp,
                    timestamp,
                    job.job_key,
                ),
            ).rowcount
            if updated != 1:
                raise SchedulerStateError("scheduled job is no longer running")
            connection.commit()

    def record_failure(
        self,
        job: ScheduledJob,
        schedule: ScheduleConfig,
        *,
        now: datetime,
        outcome: str,
        retryable: bool,
        attempt_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> str:
        inside_grace = schedule.schedule_version == 2 and now < schedule.grace_deadline(
            job.scheduled_date
        )
        should_retry = retryable and (job.attempt_count < schedule.max_attempts or inside_grace)
        status = "retry_wait" if should_retry else "failed"
        next_attempt_at = (
            now + timedelta(seconds=schedule.retry_delay_seconds(job.attempt_count))
            if should_retry
            else None
        )
        timestamp = _datetime_text(now)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE scheduled_job
                SET status = ?,
                    lease_until = NULL,
                    next_attempt_at = ?,
                    last_outcome = ?,
                    checkpoint_before = ?,
                    attempt_id = ?,
                    run_id = ?,
                    prepared_request = CASE WHEN ? = 'failed' THEN NULL ELSE prepared_request END,
                    updated_at = ?,
                    completed_at = ?
                WHERE job_key = ? AND status = 'running'
                """,
                (
                    status,
                    _optional_datetime_text(next_attempt_at),
                    outcome,
                    _optional_date_text(job.checkpoint_before),
                    str(attempt_id) if attempt_id else None,
                    str(run_id) if run_id else None,
                    status,
                    timestamp,
                    timestamp if status == "failed" else None,
                    job.job_key,
                ),
            ).rowcount
            if updated != 1:
                raise SchedulerStateError("scheduled job is no longer running")
            connection.commit()
        return status

    def status_counts(
        self,
        schedule_id: str,
        scheduled_date: date,
    ) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM scheduled_job
                WHERE schedule_id = ? AND scheduled_date = ?
                GROUP BY status
                """,
                (schedule_id, scheduled_date.isoformat()),
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def checkpoint(self, universe: SymbolUniverse, symbol: str) -> date | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT trade_date
                FROM symbol_checkpoint
                    WHERE provider = ? AND dataset_key = ? AND universe_id = ? AND universe_version = ? AND symbol = ?
                """,
                (
                    universe.provider,
                    universe.dataset_key,
                    universe.universe_id,
                    universe.universe_version,
                    symbol,
                ),
            ).fetchone()
        return date.fromisoformat(row["trade_date"]) if row else None

    def _initialize(self) -> None:
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, _SCHEMA_VERSION):
                raise SchedulerStateError(
                    f"unsupported scheduler state schema {version}; expected {_SCHEMA_VERSION}"
                )
            if version == 0:
                connection.executescript("""
                    CREATE TABLE scheduled_job (
                        job_key TEXT PRIMARY KEY,
                        schedule_id TEXT NOT NULL,
                        scheduled_date TEXT NOT NULL,
                        universe_id TEXT NOT NULL,
                        universe_version INTEGER NOT NULL,
                        symbol TEXT NOT NULL,
                        canonical_symbol TEXT NOT NULL,
                        exchange TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('pending', 'running', 'retry_wait', 'completed', 'failed')
                        ),
                        attempt_count INTEGER NOT NULL,
                        next_attempt_at TEXT,
                        lease_until TEXT,
                        last_outcome TEXT,
                        record_count INTEGER,
                        checkpoint_before TEXT,
                        checkpoint_after TEXT,
                        attempt_id TEXT,
                        run_id TEXT,
                        request_key TEXT,
                        idempotency_key TEXT,
                        prepared_request TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        UNIQUE (schedule_id, scheduled_date, symbol)
                    );
                    CREATE INDEX ix_scheduled_job_due
                    ON scheduled_job (schedule_id, status, next_attempt_at, scheduled_date);

                    CREATE TABLE symbol_checkpoint (
                        universe_id TEXT NOT NULL,
                        universe_version INTEGER NOT NULL,
                        symbol TEXT NOT NULL,
                        trade_date TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (universe_id, universe_version, symbol)
                    );
                    PRAGMA user_version = 1;
                """)
                connection.commit()
                version = 1
            if version == 1:
                self._migrate_v1(connection)
            self._validate_schema(connection)
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise SchedulerStateError(
                f"unable to secure scheduler state file: {self.path}"
            ) from exc

    def _migrate_v1(self, connection: sqlite3.Connection) -> None:
        """Atomically retain v1 jobs, request snapshots, leases, and checkpoints."""
        try:
            self._validate_v1_schema(connection)
            connection.executescript("""
                BEGIN IMMEDIATE;
                ALTER TABLE scheduled_job RENAME TO scheduled_job_v1;
                ALTER TABLE symbol_checkpoint RENAME TO symbol_checkpoint_v1;
                CREATE TABLE scheduled_job (
                    job_key TEXT PRIMARY KEY, slot_id TEXT NOT NULL DEFAULT 'legacy', provider TEXT NOT NULL DEFAULT 'twelve_data',
                    dataset_key TEXT NOT NULL DEFAULT 'us_equity_eod', work_item TEXT NOT NULL DEFAULT '', target_data_date TEXT NOT NULL DEFAULT '1970-01-01',
                    schedule_id TEXT NOT NULL, scheduled_date TEXT NOT NULL, universe_id TEXT NOT NULL,
                    universe_version INTEGER NOT NULL, symbol TEXT NOT NULL, canonical_symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending','running','retry_wait','completed','failed')),
                    attempt_count INTEGER NOT NULL, next_attempt_at TEXT, lease_until TEXT, last_outcome TEXT,
                    record_count INTEGER, checkpoint_before TEXT, checkpoint_after TEXT, attempt_id TEXT,
                    run_id TEXT, request_key TEXT, idempotency_key TEXT, prepared_request TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
                    UNIQUE(slot_id, provider, dataset_key, work_item, target_data_date)
                );
                INSERT INTO scheduled_job SELECT job_key, 'legacy', 'twelve_data', 'us_equity_eod', symbol, scheduled_date,
                    schedule_id, scheduled_date, universe_id, universe_version, symbol, canonical_symbol, exchange,
                    status, attempt_count, next_attempt_at, lease_until, last_outcome, record_count, checkpoint_before,
                    checkpoint_after, attempt_id, run_id, request_key, idempotency_key, prepared_request, created_at, updated_at, completed_at
                    FROM scheduled_job_v1;
                CREATE TABLE symbol_checkpoint (
                    provider TEXT NOT NULL, dataset_key TEXT NOT NULL, universe_id TEXT NOT NULL,
                    universe_version INTEGER NOT NULL, symbol TEXT NOT NULL, trade_date TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, dataset_key, universe_id, universe_version, symbol)
                );
                INSERT INTO symbol_checkpoint SELECT 'twelve_data', 'us_equity_eod', universe_id, universe_version, symbol, trade_date, updated_at FROM symbol_checkpoint_v1;
                DROP TABLE scheduled_job_v1; DROP TABLE symbol_checkpoint_v1;
                CREATE INDEX ix_scheduled_job_due ON scheduled_job (provider, dataset_key, slot_id, status, next_attempt_at, target_data_date);
                PRAGMA user_version = 2;
                COMMIT;
            """)
        except sqlite3.Error as exc:
            connection.rollback()
            raise SchedulerStateError("scheduler state v1 migration failed") from exc

    def _validate_v1_schema(self, connection: sqlite3.Connection) -> None:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        if not {"scheduled_job", "symbol_checkpoint"} <= tables:
            raise SchedulerStateError("scheduler state v1 schema is incomplete")

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        quick_check = [row[0] for row in connection.execute("PRAGMA quick_check").fetchall()]
        if quick_check != ["ok"]:
            raise SchedulerStateError("scheduler state integrity check failed")

        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        if not set(_EXPECTED_COLUMNS) <= tables:
            raise SchedulerStateError("scheduler state schema is incomplete")

        for table, expected in _EXPECTED_COLUMNS.items():
            actual = {
                row["name"]: (
                    str(row["type"]).upper(),
                    int(row["notnull"]),
                    int(row["pk"]),
                    int(row["hidden"]),
                )
                for row in connection.execute(f'PRAGMA table_xinfo("{table}")').fetchall()
            }
            expected_with_visibility = {
                name: (*definition, 0) for name, definition in expected.items()
            }
            if actual != expected_with_visibility:
                raise SchedulerStateError("scheduler state schema is incompatible")

        scheduled_table = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'scheduled_job'"
        ).fetchone()
        scheduled_sql = scheduled_table["sql"] if scheduled_table else ""
        status_constraint = re.compile(
            r"check\s*\(\s*status\s+in\s*\(\s*'pending'\s*,\s*'running'\s*,"
            r"\s*'retry_wait'\s*,\s*'completed'\s*,\s*'failed'\s*\)\s*\)",
            re.IGNORECASE,
        )
        if not isinstance(scheduled_sql, str) or status_constraint.search(scheduled_sql) is None:
            raise SchedulerStateError("scheduler state status constraint is missing")

        scheduled_indexes = _index_definitions(connection, "scheduled_job")
        if scheduled_indexes.get("ix_scheduled_job_due") != (
            False,
            False,
            (
                ("provider", False, "BINARY"),
                ("dataset_key", False, "BINARY"),
                ("slot_id", False, "BINARY"),
                ("status", False, "BINARY"),
                ("next_attempt_at", False, "BINARY"),
                ("target_data_date", False, "BINARY"),
            ),
        ):
            raise SchedulerStateError("scheduler state due index is missing")
        if not any(
            unique
            and not partial
            and key_columns
            == (
                ("slot_id", False, "BINARY"),
                ("provider", False, "BINARY"),
                ("dataset_key", False, "BINARY"),
                ("work_item", False, "BINARY"),
                ("target_data_date", False, "BINARY"),
            )
            for unique, partial, key_columns in scheduled_indexes.values()
        ):
            raise SchedulerStateError("scheduler state uniqueness constraint is missing")

        checkpoint_indexes = _index_definitions(connection, "symbol_checkpoint")
        if not any(
            unique
            and not partial
            and key_columns
            == (
                ("provider", False, "BINARY"),
                ("dataset_key", False, "BINARY"),
                ("universe_id", False, "BINARY"),
                ("universe_version", False, "BINARY"),
                ("symbol", False, "BINARY"),
            )
            for unique, partial, key_columns in checkpoint_indexes.values()
        ):
            raise SchedulerStateError("scheduler checkpoint primary index is missing")

    def _connect(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise SchedulerStateError(f"unable to open scheduler state: {self.path}") from exc
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        except sqlite3.Error as exc:
            connection.rollback()
            raise SchedulerStateError(f"scheduler state operation failed: {self.path}") from exc
        finally:
            connection.close()


def _job_key(schedule: ScheduleConfig, scheduled_date: date, symbol: str) -> str:
    identity = f"{schedule.slot_id}:{schedule.provider}:{schedule.dataset_key}:{symbol}:{scheduled_date.isoformat()}".encode()
    return hashlib.sha256(identity).hexdigest()


def _index_definitions(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, tuple[bool, bool, tuple[tuple[str, bool, str], ...]]]:
    definitions: dict[str, tuple[bool, bool, tuple[tuple[str, bool, str], ...]]] = {}
    for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        name = row["name"]
        key_columns = tuple(
            (
                str(column["name"]),
                bool(column["desc"]),
                str(column["coll"]).upper(),
            )
            for column in connection.execute(f'PRAGMA index_xinfo("{name}")').fetchall()
            if bool(column["key"])
        )
        definitions[name] = (bool(row["unique"]), bool(row["partial"]), key_columns)
    return definitions


def _datetime_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SchedulerStateError("scheduler timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_datetime_text(value: datetime | None) -> str | None:
    return _datetime_text(value) if value is not None else None


def _optional_date_text(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_request(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchedulerStateError("prepared request state must be JSON text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SchedulerStateError("prepared request state contains invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise SchedulerStateError("prepared request state must be a JSON object")
    return parsed
