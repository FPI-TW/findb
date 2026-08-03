"""Strict, local-only durable state for the reviewed Shioaji staging run.

This database is deliberately independent of SchedulerState.  It records a
provider attempt before a provider process can be started, so a restart cannot
silently evade the reviewed request budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from findb_fetcher.contracts import ContractError, ContractRegistry
from findb_fetcher.providers.shioaji import (
    PAYLOAD_REASONS,
    ShioajiPayloadError,
    build_market_minute_request,
)
from findb_fetcher.raw_storage import RawStorageError, attach_raw_provenance

MAX_BYTES = 1024 * 1024
SCHEMA_VERSION = "9"
_PAYLOAD_REASON_SQL = ",".join(f"'{reason}'" for reason in sorted(PAYLOAD_REASONS))
EXPECTED_COLUMNS = {
    "meta": ("key", "value"),
    "daily_updates": (
        "daily_id",
        "target_date",
        "universe_id",
        "daily_update_id",
        "execution_date",
        "preflight_status",
        "status",
    ),
    "dataset_snapshots": ("daily_id", "dataset_key", "snapshot_id", "status"),
    "snapshot_sequences": (
        "daily_id",
        "symbol",
        "dataset_key",
        "sequence_no",
        "sequence_count",
        "attempts",
        "lease_until",
        "snapshot",
        "raw_ref",
        "raw_sha256",
        "raw_size",
        "prepared",
        "prepared_sha256",
        "source_attempt_id",
        "source_run_id",
        "terminal_status",
        "terminal_reason",
        "status",
    ),
    "provider_attempts": (
        "daily_id",
        "symbol",
        "attempt_no",
        "created_at",
        "outcome",
        "code",
        "reason",
    ),
    "limiter": ("used_at",),
}


class ShioajiStagingStateError(RuntimeError):
    pass


def _validated_payload_reason(code: str, reason: str | None) -> str | None:
    if reason is None:
        return None
    if code != "KBARS_PAYLOAD" or not isinstance(reason, str) or reason not in PAYLOAD_REASONS:
        raise ShioajiStagingStateError("payload reason is invalid")
    return reason


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ShioajiStagingStateError("state clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


class ShioajiStagingState:
    def __init__(self, path: Path) -> None:
        path = path.expanduser()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        try:
            self.contracts = ContractRegistry(_contracts_dir())
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self._init()
        except Exception:
            self.db.close()
            raise

    def _init(self) -> None:
        self.db.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_updates (
              daily_id TEXT PRIMARY KEY, target_date TEXT NOT NULL, universe_id TEXT NOT NULL,
              daily_update_id TEXT NOT NULL, execution_date TEXT NOT NULL,
              preflight_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(preflight_status IN ('pending','validated')),
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','completed','failed'))
            );
            CREATE TABLE IF NOT EXISTS dataset_snapshots (
              daily_id TEXT NOT NULL REFERENCES daily_updates(daily_id), dataset_key TEXT NOT NULL,
              snapshot_id TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','completed','failed')),
              PRIMARY KEY(daily_id,dataset_key)
            );
            CREATE TABLE IF NOT EXISTS snapshot_sequences (
              daily_id TEXT NOT NULL REFERENCES daily_updates(daily_id), symbol TEXT NOT NULL,
              dataset_key TEXT NOT NULL,
              sequence_no INTEGER NOT NULL CHECK(sequence_no BETWEEN 1 AND 99999),
              sequence_count INTEGER NOT NULL
                CHECK(sequence_count BETWEEN sequence_no AND 99999),
              attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
              lease_until TEXT, snapshot BLOB CHECK(snapshot IS NULL OR length(snapshot) BETWEEN 1 AND 1048576),
              raw_ref TEXT, raw_sha256 TEXT, raw_size INTEGER, prepared BLOB,
              prepared_sha256 TEXT,
              source_attempt_id TEXT, source_run_id TEXT,
              terminal_status TEXT CHECK(terminal_status IS NULL OR length(terminal_status) BETWEEN 1 AND 64),
              terminal_reason TEXT CHECK(terminal_reason IS NULL OR terminal_reason IN ({_PAYLOAD_REASON_SQL})),
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN (
                  'pending','acquiring','acquired','raw_uploading','raw_persisted',
                  'prepared','delivered','terminal','failed'
                )),
              CHECK(
                (raw_ref IS NULL AND raw_sha256 IS NULL AND raw_size IS NULL)
                OR (raw_ref IS NULL AND raw_sha256 IS NOT NULL AND raw_size IS NULL)
                OR (raw_ref IS NOT NULL AND raw_sha256 IS NOT NULL AND raw_size BETWEEN 1 AND 1048576)
              ),
              CHECK(prepared IS NULL OR (raw_ref IS NOT NULL AND raw_sha256 IS NOT NULL)),
              CHECK(
                (prepared IS NULL AND prepared_sha256 IS NULL)
                OR (prepared IS NOT NULL AND length(prepared_sha256) = 64)
              ),
              CHECK((source_attempt_id IS NULL) = (source_run_id IS NULL)),
              CHECK(terminal_reason IS NULL OR terminal_status='KBARS_PAYLOAD'),
              PRIMARY KEY(daily_id,symbol), UNIQUE(daily_id,dataset_key,sequence_no)
            );
            CREATE TABLE IF NOT EXISTS provider_attempts (
              daily_id TEXT NOT NULL, symbol TEXT NOT NULL,
              attempt_no INTEGER NOT NULL CHECK(attempt_no BETWEEN 1 AND 3),
              created_at TEXT NOT NULL,
              outcome TEXT NOT NULL DEFAULT 'started'
                CHECK(outcome IN ('started','success','failed')),
              code TEXT CHECK(code IS NULL OR length(code) BETWEEN 1 AND 64),
              reason TEXT CHECK(reason IS NULL OR reason IN ({_PAYLOAD_REASON_SQL})),
              CHECK(reason IS NULL OR code='KBARS_PAYLOAD'),
              PRIMARY KEY(daily_id,symbol,attempt_no),
              FOREIGN KEY(daily_id,symbol) REFERENCES snapshot_sequences(daily_id,symbol)
            );
            CREATE TABLE IF NOT EXISTS limiter (used_at TEXT NOT NULL);
            """
        )
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('schema_version',?)", (SCHEMA_VERSION,))
        elif row[0] != SCHEMA_VERSION:
            raise ShioajiStagingStateError("staging state schema is incompatible")
        for table, expected in EXPECTED_COLUMNS.items():
            columns = tuple(
                str(column[1]) for column in self.db.execute(f"PRAGMA table_info({table})")
            )
            if columns != expected:
                raise ShioajiStagingStateError("staging state schema is incompatible")
        if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ShioajiStagingStateError("staging state integrity check failed")
        self._validate_semantics()

    def bind_raw_storage(self, account_id: str, bucket: str) -> None:
        """Bind persisted delivery refs to one unambiguous Raw R2 location."""
        if (
            not isinstance(account_id, str)
            or not isinstance(bucket, str)
            or not account_id
            or not bucket
            or len(account_id) > 64
            or len(bucket) > 128
            or "\n" in account_id
            or "\n" in bucket
        ):
            raise ShioajiStagingStateError("raw storage binding is invalid")
        fingerprint = hashlib.sha256(f"{account_id}\n{bucket}".encode()).hexdigest()
        key = "raw_storage_binding_sha256"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            persisted = self.db.execute(
                "SELECT 1 FROM snapshot_sequences "
                "WHERE raw_ref IS NOT NULL OR prepared IS NOT NULL LIMIT 1"
            ).fetchone()
            if persisted is not None and (existing is None or existing[0] != fingerprint):
                raise ShioajiStagingStateError(
                    "raw storage binding conflicts with persisted delivery"
                )
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, fingerprint),
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def ensure(
        self,
        daily_id: str,
        target_date: str,
        universe_id: str,
        daily_update_id: str,
        rows: list[dict[str, Any]],
        execution_date: str,
    ) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO daily_updates(daily_id,target_date,universe_id,daily_update_id,execution_date) VALUES (?,?,?,?,?)",
                (daily_id, target_date, universe_id, daily_update_id, execution_date),
            )
            existing = self.db.execute(
                "SELECT target_date,universe_id,daily_update_id,execution_date FROM daily_updates WHERE daily_id=?",
                (daily_id,),
            ).fetchone()
            if existing != (target_date, universe_id, daily_update_id, execution_date):
                raise ShioajiStagingStateError("daily update identity changed")
            for row in rows:
                ds = str(row["dataset_key"])
                snapshot_id = _stable_id(daily_id, ds, "snapshot")
                self.db.execute(
                    "INSERT OR IGNORE INTO dataset_snapshots"
                    "(daily_id,dataset_key,snapshot_id) VALUES (?,?,?)",
                    (daily_id, ds, snapshot_id),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO snapshot_sequences(daily_id,symbol,dataset_key,sequence_no,sequence_count) VALUES (?,?,?,?,?)",
                    (
                        daily_id,
                        str(row["symbol"]),
                        ds,
                        int(row["sequence"]),
                        int(row["sequence_count"]),
                    ),
                )
            expected_snapshots = {
                str(row["dataset_key"]): _stable_id(
                    daily_id,
                    str(row["dataset_key"]),
                    "snapshot",
                )
                for row in rows
            }
            actual_snapshots = dict(
                self.db.execute(
                    "SELECT dataset_key,snapshot_id FROM dataset_snapshots WHERE daily_id=?",
                    (daily_id,),
                )
            )
            expected_sequences = {
                (
                    str(row["symbol"]),
                    str(row["dataset_key"]),
                    int(row["sequence"]),
                    int(row["sequence_count"]),
                )
                for row in rows
            }
            actual_sequences = set(
                self.db.execute(
                    "SELECT symbol,dataset_key,sequence_no,sequence_count "
                    "FROM snapshot_sequences WHERE daily_id=?",
                    (daily_id,),
                )
            )
            if actual_snapshots != expected_snapshots or actual_sequences != expected_sequences:
                raise ShioajiStagingStateError("staging plan identity changed")
            self._refresh_statuses(daily_id)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def preflight_is_fresh(self, daily_id: str, symbol: str) -> bool:
        """Allow only a brand-new database and an untouched reviewed plan."""
        rows = self.db.execute(
            "SELECT symbol,attempts,lease_until,snapshot,raw_ref,raw_sha256,raw_size,"
            "prepared,prepared_sha256,source_attempt_id,source_run_id,terminal_status,terminal_reason,status "
            "FROM snapshot_sequences WHERE daily_id=? ORDER BY sequence_no,symbol",
            (daily_id,),
        ).fetchall()
        untouched = (
            rows
            and any(row[0] == symbol for row in rows)
            and all(row[1:] == (0,) + (None,) * 11 + ("pending",) for row in rows)
        )
        return bool(
            untouched
            and self.db.execute("SELECT count(*) FROM daily_updates").fetchone()[0] == 1
            and self.db.execute(
                "SELECT status FROM daily_updates WHERE daily_id=?", (daily_id,)
            ).fetchone()
            == ("pending",)
            and all(
                row == ("pending",)
                for row in self.db.execute(
                    "SELECT status FROM dataset_snapshots WHERE daily_id=?", (daily_id,)
                )
            )
            and self.db.execute("SELECT count(*) FROM provider_attempts").fetchone()[0] == 0
            and self.db.execute("SELECT count(*) FROM limiter").fetchone()[0] == 0
        )

    def preflight_validated(self, daily_id: str) -> bool:
        return self.db.execute(
            "SELECT preflight_status FROM daily_updates WHERE daily_id=?", (daily_id,)
        ).fetchone() == ("validated",)

    def mark_preflight_validated(self, daily_id: str, symbol: str) -> None:
        """Persist the Phase 4 gate only after the 2330 snapshot validates."""
        row = self.db.execute(
            "SELECT attempts,snapshot,terminal_status,status FROM snapshot_sequences "
            "WHERE daily_id=? AND symbol=?",
            (daily_id, symbol),
        ).fetchone()
        if (
            row is None
            or row[0] != 1
            or row[1] is None
            or row[2] is not None
            or row[3] != "acquired"
        ):
            raise ShioajiStagingStateError("preflight state is invalid")
        if (
            self.db.execute(
                "UPDATE daily_updates SET preflight_status='validated' "
                "WHERE daily_id=? AND preflight_status='pending'",
                (daily_id,),
            ).rowcount
            != 1
        ):
            raise ShioajiStagingStateError("preflight state is invalid")

    def sequence(self, daily_id: str, symbol: str) -> sqlite3.Row | tuple[Any, ...] | None:
        return self.db.execute(
            "SELECT s.*, d.snapshot_id FROM snapshot_sequences s JOIN dataset_snapshots d ON d.daily_id=s.daily_id AND d.dataset_key=s.dataset_key WHERE s.daily_id=? AND s.symbol=?",
            (daily_id, symbol),
        ).fetchone()

    def acquire_attempt(
        self,
        daily_id: str,
        symbol: str,
        now: datetime,
        *,
        max_requests: int,
        rolling_seconds: int,
        max_attempts: int,
        lease_seconds: int = 120,
        terminal_on_exhaustion: bool = False,
    ) -> tuple[bool, int]:
        now_text = _utc(now)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cutoff = _utc(now - timedelta(seconds=rolling_seconds))
            self.db.execute("DELETE FROM limiter WHERE used_at < ?", (cutoff,))
            attempts, lease, terminal = self.db.execute(
                "SELECT attempts,lease_until,terminal_status FROM snapshot_sequences WHERE daily_id=? AND symbol=?",
                (daily_id, symbol),
            ).fetchone() or (-1, None, "blocked")
            used = self.db.execute("SELECT count(*) FROM limiter").fetchone()[0]
            if (
                terminal_on_exhaustion
                and attempts >= max_attempts
                and terminal is None
                and not (lease and lease > now_text)
            ):
                # A recurring production poll must not leave a retryable
                # sequence in an endless ATTEMPT_BLOCKED state.  Staging
                # callers retain the historical blocked response unless they
                # explicitly opt into this terminal transition.
                self.db.execute(
                    "UPDATE snapshot_sequences SET terminal_status=?,"
                    "status='terminal',lease_until=NULL "
                    "WHERE daily_id=? AND symbol=? AND terminal_status IS NULL",
                    ("ACQUISITION_ATTEMPTS_EXHAUSTED", daily_id, symbol),
                )
                self._refresh_statuses(daily_id)
                self.db.execute("COMMIT")
                return False, max(attempts, 0)
            if (
                attempts < 0
                or terminal
                or attempts >= max_attempts
                or used >= max_requests
                or (lease and lease > now_text)
            ):
                self.db.execute("COMMIT")
                return False, max(attempts, 0)
            number = attempts + 1
            self.db.execute(
                "UPDATE snapshot_sequences SET attempts=?,lease_until=?,status='acquiring' WHERE daily_id=? AND symbol=?",
                (number, _utc(now + timedelta(seconds=lease_seconds)), daily_id, symbol),
            )
            self.db.execute(
                "INSERT INTO provider_attempts(daily_id,symbol,attempt_no,created_at) VALUES (?,?,?,?)",
                (daily_id, symbol, number, now_text),
            )
            self.db.execute("INSERT INTO limiter VALUES (?)", (now_text,))
            self.db.execute("COMMIT")
            return True, number
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def finish_attempt(
        self,
        daily_id: str,
        symbol: str,
        attempt: int,
        outcome: str,
        code: str,
        *,
        retryable: bool,
        reason: str | None = None,
    ) -> None:
        reason = _validated_payload_reason(code, reason)
        status = "acquired" if outcome == "success" else ("pending" if retryable else "failed")
        terminal_code = None if outcome == "success" or retryable else code
        self.db.execute("BEGIN IMMEDIATE")
        try:
            updated = self.db.execute(
                "UPDATE provider_attempts SET outcome=?,code=?,reason=? "
                "WHERE daily_id=? AND symbol=? AND attempt_no=? AND outcome='started'",
                (
                    outcome,
                    code,
                    reason,
                    daily_id,
                    symbol,
                    attempt,
                ),
            ).rowcount
            if updated != 1:
                raise ShioajiStagingStateError("provider attempt identity changed")
            self.db.execute(
                "UPDATE snapshot_sequences SET lease_until=NULL,status=?,"
                "terminal_status=COALESCE(?,terminal_status),terminal_reason=COALESCE(?,terminal_reason) "
                "WHERE daily_id=? AND symbol=?",
                (
                    status,
                    terminal_code,
                    reason,
                    daily_id,
                    symbol,
                ),
            )
            self._refresh_statuses(daily_id)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def complete_attempt_with_snapshot(
        self,
        daily_id: str,
        symbol: str,
        attempt: int,
        snapshot: bytes,
    ) -> None:
        if not snapshot or len(snapshot) > MAX_BYTES:
            raise ShioajiStagingStateError("snapshot exceeds limit")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            updated = self.db.execute(
                "UPDATE provider_attempts SET outcome='success',code='ACQUIRED',reason=NULL "
                "WHERE daily_id=? AND symbol=? AND attempt_no=? AND outcome='started'",
                (daily_id, symbol, attempt),
            ).rowcount
            if updated != 1:
                raise ShioajiStagingStateError("provider attempt identity changed")
            self.db.execute(
                "UPDATE snapshot_sequences SET snapshot=?,lease_until=NULL,status='acquired' "
                "WHERE daily_id=? AND symbol=?",
                (snapshot, daily_id, symbol),
            )
            self._refresh_statuses(daily_id)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def get_snapshot(self, daily_id: str, symbol: str) -> bytes | None:
        row = self.db.execute(
            "SELECT snapshot FROM snapshot_sequences WHERE daily_id=? AND symbol=?",
            (daily_id, symbol),
        ).fetchone()
        return row[0] if row else None

    def mark_terminal(
        self, daily_id: str, symbol: str, code: str, *, reason: str | None = None
    ) -> None:
        if not code or len(code) > 64:
            raise ShioajiStagingStateError("terminal code is invalid")
        reason = _validated_payload_reason(code, reason)
        self.db.execute(
            "UPDATE snapshot_sequences SET terminal_status=?,terminal_reason=?,status='terminal',"
            "lease_until=NULL WHERE daily_id=? AND symbol=?",
            (
                code,
                reason,
                daily_id,
                symbol,
            ),
        )
        self._refresh_statuses(daily_id)

    def get_terminal(self, daily_id: str, symbol: str) -> tuple[str, str | None] | None:
        row = self.db.execute(
            "SELECT terminal_status,terminal_reason FROM snapshot_sequences WHERE daily_id=? AND symbol=?",
            (daily_id, symbol),
        ).fetchone()
        return (
            (str(row[0]), str(row[1]) if row[1] is not None else None)
            if row and row[0] is not None
            else None
        )

    def mark_cutoff(self, daily_id: str) -> int:
        """Atomically fail unfinished work when a same-day production window closes."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "UPDATE provider_attempts SET outcome='failed',code='CUTOFF_REACHED' "
                "WHERE daily_id=? AND outcome='started'",
                (daily_id,),
            )
            updated = self.db.execute(
                "UPDATE snapshot_sequences SET terminal_status='CUTOFF_REACHED',"
                "terminal_reason=NULL,status='terminal',lease_until=NULL "
                "WHERE daily_id=? AND terminal_status IS NULL",
                (daily_id,),
            ).rowcount
            if updated:
                self._refresh_statuses(daily_id)
            self.db.execute("COMMIT")
            return updated
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def save_prepared(
        self,
        daily_id: str,
        symbol: str,
        body: bytes,
        raw_ref: str | None = None,
        raw_sha256: str | None = None,
    ) -> None:
        if not body or len(body) > MAX_BYTES:
            raise ShioajiStagingStateError("prepared request exceeds limit")
        try:
            value = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise ShioajiStagingStateError("prepared request is invalid") from exc
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("idempotency_key"), str)
            or not isinstance(value.get("dataset_key"), str)
            or not isinstance(value.get("schema_id"), str)
            or type(value.get("schema_version")) is not int
        ):
            raise ShioajiStagingStateError("prepared request is invalid")
        self._validate_prepared(daily_id, symbol, body)
        digest = hashlib.sha256(body).hexdigest()
        self.db.execute(
            "UPDATE snapshot_sequences SET prepared=COALESCE(prepared,?),"
            "prepared_sha256=COALESCE(prepared_sha256,?),"
            "raw_ref=COALESCE(raw_ref,?),raw_sha256=COALESCE(raw_sha256,?),"
            "status='prepared' WHERE daily_id=? AND symbol=?",
            (body, digest, raw_ref, raw_sha256, daily_id, symbol),
        )
        if self.get_prepared(daily_id, symbol) != body:
            raise ShioajiStagingStateError("prepared request identity changed")
        self._refresh_statuses(daily_id)

    def get_prepared(self, daily_id: str, symbol: str) -> bytes | None:
        row = self.db.execute(
            "SELECT prepared,prepared_sha256 FROM snapshot_sequences WHERE daily_id=? AND symbol=?",
            (daily_id, symbol),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        body = bytes(row[0])
        if row[1] != hashlib.sha256(body).hexdigest():
            raise ShioajiStagingStateError("prepared request checksum changed")
        self._validate_prepared(daily_id, symbol, body)
        return body

    def save_raw(
        self,
        daily_id: str,
        symbol: str,
        ref: str,
        sha256: str,
        size_bytes: int,
    ) -> None:
        if (
            not ref
            or len(ref) > 2048
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or type(size_bytes) is not int
            or size_bytes < 1
            or size_bytes > MAX_BYTES
        ):
            raise ShioajiStagingStateError("raw object metadata is invalid")
        self.db.execute(
            "UPDATE snapshot_sequences SET raw_ref=COALESCE(raw_ref,?),"
            "raw_sha256=COALESCE(raw_sha256,?),raw_size=COALESCE(raw_size,?),"
            "status='raw_persisted' WHERE daily_id=? AND symbol=?",
            (ref, sha256, size_bytes, daily_id, symbol),
        )
        if self.get_raw(daily_id, symbol) != (ref, sha256, size_bytes):
            raise ShioajiStagingStateError("raw object identity changed")
        self._refresh_statuses(daily_id)

    def begin_raw(self, daily_id: str, symbol: str, snapshot: bytes) -> str:
        digest = hashlib.sha256(snapshot).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT raw_ref,raw_sha256,raw_size FROM snapshot_sequences "
                "WHERE daily_id=? AND symbol=?",
                (daily_id, symbol),
            ).fetchone()
            if row is None:
                raise ShioajiStagingStateError("raw sequence is missing")
            if row == (None, None, None):
                self.db.execute(
                    "UPDATE snapshot_sequences SET raw_sha256=?,status='raw_uploading' "
                    "WHERE daily_id=? AND symbol=?",
                    (digest, daily_id, symbol),
                )
                self.db.execute("COMMIT")
                return "started"
            if row == (None, digest, None):
                self.db.execute("COMMIT")
                return "uncertain"
            if row[0] is not None and row[1] == digest and row[2] is not None:
                self.db.execute("COMMIT")
                return "complete"
            raise ShioajiStagingStateError("raw object identity changed")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def get_raw(self, daily_id: str, symbol: str) -> tuple[str, str, int] | None:
        row = self.db.execute(
            "SELECT raw_ref,raw_sha256,raw_size FROM snapshot_sequences "
            "WHERE daily_id=? AND symbol=?",
            (daily_id, symbol),
        ).fetchone()
        if row is None or any(value is None for value in row):
            return None
        return str(row[0]), str(row[1]), int(row[2])

    def save_source(
        self, daily_id: str, symbol: str, attempt_id: str, run_id: str, terminal: str | None = None
    ) -> None:
        self.db.execute(
            "UPDATE snapshot_sequences SET source_attempt_id=?,source_run_id=?,terminal_status=COALESCE(?,terminal_status),status=CASE WHEN ? IS NULL THEN 'delivered' ELSE 'terminal' END WHERE daily_id=? AND symbol=?",
            (attempt_id, run_id, terminal, terminal, daily_id, symbol),
        )
        self._refresh_statuses(daily_id)

    def _refresh_statuses(self, daily_id: str) -> None:
        datasets = self.db.execute(
            "SELECT dataset_key FROM dataset_snapshots WHERE daily_id=?",
            (daily_id,),
        ).fetchall()
        for (dataset_key,) in datasets:
            rows = list(
                self.db.execute(
                    "SELECT status,terminal_status FROM snapshot_sequences "
                    "WHERE daily_id=? AND dataset_key=?",
                    (daily_id, dataset_key),
                )
            )
            if any(
                status == "failed" or (status == "terminal" and terminal_status != "completed")
                for status, terminal_status in rows
            ):
                aggregate = "failed"
            elif rows and all(
                status == "terminal" and terminal_status == "completed"
                for status, terminal_status in rows
            ):
                aggregate = "completed"
            elif any(status != "pending" for status, _ in rows):
                aggregate = "running"
            else:
                aggregate = "pending"
            self.db.execute(
                "UPDATE dataset_snapshots SET status=? WHERE daily_id=? AND dataset_key=?",
                (aggregate, daily_id, dataset_key),
            )
        dataset_statuses = [
            str(row[0])
            for row in self.db.execute(
                "SELECT status FROM dataset_snapshots WHERE daily_id=?",
                (daily_id,),
            )
        ]
        self.db.execute(
            "UPDATE daily_updates SET status=? WHERE daily_id=?",
            (_aggregate_status(dataset_statuses), daily_id),
        )

    def _validate_semantics(self) -> None:
        for target_date, execution_date in self.db.execute(
            "SELECT target_date,execution_date FROM daily_updates"
        ):
            try:
                parsed_target = date.fromisoformat(str(target_date))
                parsed_execution = date.fromisoformat(str(execution_date))
            except ValueError as exc:
                raise ShioajiStagingStateError("staging state semantics are invalid") from exc
            if not parsed_execution - timedelta(days=31) <= parsed_target <= parsed_execution:
                raise ShioajiStagingStateError("staging state semantics are invalid")
        for (
            daily_id,
            symbol,
            attempts,
            lease_until,
            status,
            snapshot,
            raw_ref,
            raw_sha256,
            raw_size,
            prepared,
            prepared_sha256,
        ) in self.db.execute(
            "SELECT daily_id,symbol,attempts,lease_until,status,snapshot,"
            "raw_ref,raw_sha256,raw_size,prepared,prepared_sha256 "
            "FROM snapshot_sequences"
        ):
            if (
                type(attempts) is not int
                or not 0 <= attempts <= 3
                or status
                not in {
                    "pending",
                    "acquiring",
                    "acquired",
                    "raw_uploading",
                    "raw_persisted",
                    "prepared",
                    "delivered",
                    "terminal",
                    "failed",
                }
            ):
                raise ShioajiStagingStateError("staging state semantics are invalid")
            if lease_until is not None:
                try:
                    parsed_lease = datetime.fromisoformat(str(lease_until))
                except ValueError as exc:
                    raise ShioajiStagingStateError("staging state semantics are invalid") from exc
                if parsed_lease.tzinfo is None:
                    raise ShioajiStagingStateError("staging state semantics are invalid")
            attempt_numbers = [
                int(row[0])
                for row in self.db.execute(
                    "SELECT attempt_no FROM provider_attempts "
                    "WHERE daily_id=? AND symbol=? ORDER BY attempt_no",
                    (daily_id, symbol),
                )
            ]
            if attempt_numbers != list(range(1, attempts + 1)):
                raise ShioajiStagingStateError("staging state semantics are invalid")
            if raw_sha256 is not None and (
                not isinstance(raw_sha256, str)
                or len(raw_sha256) != 64
                or any(character not in "0123456789abcdef" for character in raw_sha256)
            ):
                raise ShioajiStagingStateError("staging state semantics are invalid")
            if snapshot is not None and raw_sha256 is not None:
                if hashlib.sha256(bytes(snapshot)).hexdigest() != raw_sha256:
                    raise ShioajiStagingStateError("staging state semantics are invalid")
            if raw_ref is not None and (not isinstance(raw_ref, str) or len(raw_ref) > 2048):
                raise ShioajiStagingStateError("staging state semantics are invalid")
            if raw_size is not None and snapshot is not None and raw_size != len(snapshot):
                raise ShioajiStagingStateError("staging state semantics are invalid")
            if prepared is not None:
                if prepared_sha256 != hashlib.sha256(bytes(prepared)).hexdigest():
                    raise ShioajiStagingStateError("staging state semantics are invalid")
                self._validate_prepared(str(daily_id), str(symbol), bytes(prepared))

    def _validate_prepared(self, daily_id: str, symbol: str, body: bytes) -> None:
        try:
            value = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise ShioajiStagingStateError("prepared request is invalid") from exc
        row = self.db.execute(
            "SELECT s.dataset_key,s.sequence_no,s.sequence_count,d.snapshot_id,"
            "u.target_date,u.universe_id,u.daily_update_id,s.raw_ref,s.raw_sha256,"
            "s.snapshot "
            "FROM snapshot_sequences s "
            "JOIN dataset_snapshots d ON d.daily_id=s.daily_id AND d.dataset_key=s.dataset_key "
            "JOIN daily_updates u ON u.daily_id=s.daily_id "
            "WHERE s.daily_id=? AND s.symbol=?",
            (daily_id, symbol),
        ).fetchone()
        payload = value.get("payload") if isinstance(value, dict) else None
        batch = payload.get("batch") if isinstance(payload, dict) else None
        data = payload.get("data") if isinstance(payload, dict) else None
        if (
            row is None
            or not isinstance(batch, dict)
            or not isinstance(data, list)
            or not data
            or batch.get("declared_record_count") != len(data)
            or any(not isinstance(item, dict) or item.get("symbol") != symbol for item in data)
        ):
            raise ShioajiStagingStateError("prepared request identity changed")
        try:
            self.contracts.validate("market_minute", 1, value)
        except ContractError as exc:
            raise ShioajiStagingStateError("prepared request contract is invalid") from exc
        (
            dataset_key,
            sequence_no,
            sequence_count,
            snapshot_id,
            target_date,
            universe_id,
            daily_update_id,
            raw_ref,
            raw_sha256,
            snapshot,
        ) = row
        digest = hashlib.sha256(
            json.dumps(
                {
                    "data_date": target_date,
                    "dataset_key": dataset_key,
                    "sequence": sequence_no,
                    "snapshot_id": snapshot_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if (
            value.get("dataset_key") != dataset_key
            or value.get("schema_id") != "market_minute"
            or value.get("schema_version") != 1
            or value.get("request_key") != f"mmr:{digest}"
            or value.get("idempotency_key") != f"mms:{digest}"
            or batch.get("data_date") != target_date
            or batch.get("sequence") != sequence_no
            or batch.get("sequence_count") != sequence_count
            or batch.get("snapshot_id") != snapshot_id
            or batch.get("daily_update_id") != daily_update_id
            or batch.get("universe_id") != universe_id
            or batch.get("source_raw_ref") != raw_ref
            or batch.get("source_raw_sha256") != raw_sha256
        ):
            raise ShioajiStagingStateError("prepared request identity changed")
        if snapshot is None:
            raise ShioajiStagingStateError("prepared request snapshot is missing")
        try:
            detached = json.loads(bytes(snapshot))
            kbars = detached["kbars"]
            if detached.get("kind") != "shioaji_sdk_acquisition_snapshot.v1" or not isinstance(
                kbars, dict
            ):
                raise ShioajiPayloadError("snapshot invalid")
            fetched_at = datetime.fromisoformat(str(value["fetched_at"]).replace("Z", "+00:00"))
            usage_before = batch["provider_usage_before"]["requests_used"]
            usage_after = batch["provider_usage_after"]["requests_used"]
            expected = build_market_minute_request(
                {str(key): tuple(items) for key, items in kbars.items()},
                dataset_key=str(dataset_key),
                target_date=datetime.fromisoformat(str(target_date)).date(),
                symbols=(symbol,),
                fetched_at=fetched_at,
                usage_before_requests=usage_before,
                usage_after_requests=usage_after,
                snapshot_id=str(snapshot_id),
                daily_update_id=str(daily_update_id),
                universe_id=str(universe_id),
                sequence=int(sequence_no),
                sequence_count=int(sequence_count),
            )
            attach_raw_provenance(
                expected,
                source_raw_ref=str(raw_ref),
                source_raw_sha256=str(raw_sha256),
                preserve_identity=True,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            ShioajiPayloadError,
            RawStorageError,
        ) as exc:
            raise ShioajiStagingStateError("prepared request snapshot is invalid") from exc
        if expected != value:
            raise ShioajiStagingStateError("prepared request content changed")

    def close(self) -> None:
        self.db.close()


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def _aggregate_status(statuses: list[str]) -> str:
    if any(status == "failed" for status in statuses):
        return "failed"
    if statuses and all(status == "completed" for status in statuses):
        return "completed"
    if any(status != "pending" for status in statuses):
        return "running"
    return "pending"


def _contracts_dir() -> Path:
    configured = os.getenv("FETCHER_CONTRACTS_DIR")
    if configured:
        return Path(configured)
    container_path = Path("/app/contracts")
    if container_path.is_dir():
        return container_path
    repository_path = Path(__file__).resolve().parents[3] / "contracts"
    if repository_path.is_dir():
        return repository_path
    return Path.cwd() / "contracts"
