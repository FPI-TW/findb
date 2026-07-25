"""Fetcher-owned SQLite state for schedules, retries, leases, and checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
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

_SCHEMA_VERSION = 1


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
        now: datetime,
    ) -> int:
        timestamp = _datetime_text(now)
        inserted = 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for member in universe.symbols:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO scheduled_job (
                        job_key, schedule_id, scheduled_date, universe_id,
                        universe_version, symbol, canonical_symbol, exchange,
                        status, attempt_count, next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                    """,
                    (
                        _job_key(schedule.schedule_id, scheduled_date, member.symbol),
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
                SELECT job_key, schedule_id, scheduled_date, universe_id,
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
                    WHERE universe_id = ? AND universe_version = ? AND symbol = ?
                    """,
                    (row["universe_id"], row["universe_version"], row["symbol"]),
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
                        universe_id, universe_version, symbol, trade_date, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (universe_id, universe_version, symbol)
                    DO UPDATE SET trade_date = excluded.trade_date, updated_at = excluded.updated_at
                    WHERE excluded.trade_date > symbol_checkpoint.trade_date
                    """,
                    (
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
        should_retry = retryable and job.attempt_count < schedule.max_attempts
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
                WHERE universe_id = ? AND universe_version = ? AND symbol = ?
                """,
                (universe.universe_id, universe.universe_version, symbol),
            ).fetchone()
        return date.fromisoformat(row["trade_date"]) if row else None

    def _initialize(self) -> None:
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, _SCHEMA_VERSION):
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
        try:
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise SchedulerStateError(
                f"unable to secure scheduler state file: {self.path}"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
        except sqlite3.Error as exc:
            raise SchedulerStateError(f"unable to open scheduler state: {self.path}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
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


def _job_key(schedule_id: str, scheduled_date: date, symbol: str) -> str:
    identity = f"{schedule_id}:{scheduled_date.isoformat()}:{symbol}".encode()
    return hashlib.sha256(identity).hexdigest()


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
