"""Shared SQLite reservations, work leases and immutable full-market prepared bodies."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from findb_fetcher.full_market_universe import canonical_bytes, checksum

FULL_WORK_MAX_ATTEMPTS = 8


class FullMarketStateError(RuntimeError):
    pass


class QuotaBlockedError(FullMarketStateError):
    # Set at the real acquisition boundary when the provider might have run.
    # Pre-dispatch waiting cannot spend an operation failure budget.
    dispatched = False

    def __init__(
        self, message: str, *, quota_wait: bool = False, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.quota_wait = quota_wait
        self.status_code = status_code


class FullMarketState:
    def __init__(self, path: Path, *, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or (lambda: time.time())
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        if path.is_symlink() or path.parent.is_symlink():
            raise FullMarketStateError("state path must not be a symlink")
        if not path.exists():
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        os.chmod(path, 0o600)
        from findb_fetcher.acquisition import account_lock

        with account_lock(path, "schema"), self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS full_account_identity (provider TEXT NOT NULL, credential_sha256 TEXT NOT NULL, account TEXT NOT NULL, PRIMARY KEY(provider,credential_sha256));
                CREATE TABLE IF NOT EXISTS full_quota (account TEXT, window TEXT, requests INTEGER NOT NULL, bytes INTEGER NOT NULL, blocked_until REAL NOT NULL DEFAULT 0, PRIMARY KEY(account,window));
                CREATE TABLE IF NOT EXISTS full_pacing (account TEXT PRIMARY KEY, next_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS full_account_cooldown(account TEXT PRIMARY KEY, not_before REAL NOT NULL, reason TEXT NOT NULL);
                INSERT INTO full_account_cooldown SELECT account,next_at,'legacy' FROM full_pacing WHERE true ON CONFLICT(account) DO UPDATE SET not_before=MAX(not_before,excluded.not_before),reason=CASE WHEN excluded.not_before>not_before THEN 'legacy' ELSE reason END;
                CREATE TABLE IF NOT EXISTS full_capacity_violation (account TEXT PRIMARY KEY, violation_id TEXT NOT NULL, observed_bytes INTEGER NOT NULL, allocation_sha256 TEXT NOT NULL, recorded_at REAL NOT NULL, received_bytes INTEGER NOT NULL DEFAULT 0, declared_bytes INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS full_capacity_repair (violation_id TEXT PRIMARY KEY, account TEXT NOT NULL, observed_bytes INTEGER NOT NULL, previous_allocation_sha256 TEXT NOT NULL, corrected_allocation_sha256 TEXT NOT NULL, declaration_sha256 TEXT NOT NULL, repaired_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS full_work (key TEXT PRIMARY KEY, provider TEXT NOT NULL, plan_id TEXT NOT NULL, member_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0, body BLOB, body_sha256 TEXT, receipt TEXT, reason TEXT, next_at REAL NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS full_work_prepared_drain ON full_work(provider,plan_id,key) WHERE body IS NOT NULL AND status IN ('pending','prepared');
                CREATE INDEX IF NOT EXISTS full_work_plan_status ON full_work(plan_id,status);
                CREATE INDEX IF NOT EXISTS full_work_provider_status ON full_work(provider,status);
                CREATE TABLE IF NOT EXISTS full_plan (plan_id TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset TEXT NOT NULL, trade_date TEXT NOT NULL, body BLOB NOT NULL, complete INTEGER NOT NULL DEFAULT 0, late INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS full_plan_reconcile (plan_id TEXT PRIMARY KEY, dirty INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS full_plan_reconcile_dirty ON full_plan_reconcile(dirty,plan_id);
                INSERT OR IGNORE INTO full_plan_reconcile SELECT plan_id,1 FROM full_plan WHERE complete=0;
                CREATE TABLE IF NOT EXISTS full_work_receipt (key TEXT NOT NULL, idempotency_key TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(key,idempotency_key));
                CREATE TABLE IF NOT EXISTS full_refresh (provider TEXT PRIMARY KEY, next_at REAL NOT NULL, result BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS full_date_prerequisite (provider TEXT NOT NULL, dataset TEXT NOT NULL, trade_date TEXT NOT NULL, reason TEXT NOT NULL, next_at REAL NOT NULL, PRIMARY KEY(provider,dataset,trade_date));
                CREATE TABLE IF NOT EXISTS full_cursor (provider TEXT, dataset TEXT, trade_date TEXT NOT NULL, PRIMARY KEY(provider,dataset));
                CREATE TABLE IF NOT EXISTS full_schedule (scope TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0, batch BLOB);
                CREATE TABLE IF NOT EXISTS acquisition_intent(operation_id TEXT PRIMARY KEY, account TEXT NOT NULL, provider TEXT NOT NULL, armed_at REAL NOT NULL, status TEXT NOT NULL, cooldown_bound REAL NOT NULL, actual_start REAL, completed_at REAL);
                CREATE TABLE IF NOT EXISTS acquisition_usage(operation_id TEXT NOT NULL, observed_at REAL NOT NULL, bytes INTEGER NOT NULL, limit_bytes INTEGER NOT NULL, remaining_bytes INTEGER NOT NULL, PRIMARY KEY(operation_id,observed_at));
                CREATE TABLE IF NOT EXISTS account_alias_audit(alias TEXT PRIMARY KEY, account TEXT NOT NULL, original_quota TEXT NOT NULL, original_pacing REAL NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(full_capacity_violation)")}
            for column in ("received_bytes", "declared_bytes"):
                if column not in columns:
                    db.execute(
                        f"ALTER TABLE full_capacity_violation ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
            for table, additions in {
                "acquisition_intent": {
                    "operation_kind": "TEXT NOT NULL DEFAULT 'acquisition'",
                    "request_units": "INTEGER NOT NULL DEFAULT 1 CHECK(request_units>=0)",
                    "login_units": "INTEGER NOT NULL DEFAULT 0 CHECK(login_units IN (0,1))",
                    "usage_before_state": "TEXT NOT NULL DEFAULT 'unknown'",
                    "outcome": "TEXT NOT NULL DEFAULT 'unknown'",
                },
                "acquisition_usage": {"connections": "INTEGER", "sample_started_at": "REAL"},
            }.items():
                current = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, definition in additions.items():
                    if column not in current:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            db.execute(
                "CREATE INDEX IF NOT EXISTS acquisition_login_window ON acquisition_intent(account,operation_kind,actual_start)"
            )
            # Promote cooldowns written by older runtimes out of dated quota rows.
            for row in db.execute(
                "SELECT account,MAX(blocked_until) FROM full_quota WHERE blocked_until>0 GROUP BY account"
            ).fetchall():
                self.record_floor(db, row[0], row[1], reason="quota_block")

    @staticmethod
    def record_floor(db, account: str, until: float, *, reason: str) -> None:
        """Persist pacing and its reason atomically; longer debt always wins."""
        db.execute(
            "INSERT INTO full_pacing VALUES(?,?) ON CONFLICT(account) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)",
            (account, until),
        )
        db.execute(
            "INSERT INTO full_account_cooldown VALUES(?,?,?) ON CONFLICT(account) DO UPDATE SET not_before=MAX(not_before,excluded.not_before),reason=CASE WHEN excluded.not_before>=not_before THEN excluded.reason ELSE reason END",
            (account, until, reason),
        )

    def bind_profile(self, profile: str) -> None:
        """Bind a checkpoint once; legacy full checkpoints retain all state verbatim."""
        if profile not in {"pilot", "full_market"}:
            raise FullMarketStateError("invalid execution profile")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS execution_identity (id INTEGER PRIMARY KEY CHECK(id=1), profile TEXT NOT NULL)"
            )
            row = db.execute("SELECT profile FROM execution_identity WHERE id=1").fetchone()
            if row is not None and row[0] != profile:
                raise FullMarketStateError("checkpoint execution profile mismatch")
            if row is None:
                if profile == "pilot" and any(
                    db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                    for table in ("full_work", "full_plan", "full_cursor")
                ):
                    raise FullMarketStateError("legacy full-market checkpoint cannot become pilot")
                db.execute(
                    "INSERT INTO execution_identity(id,profile) VALUES(1,?) ON CONFLICT DO NOTHING",
                    (profile,),
                )

    @contextmanager
    def connection(self) -> Any:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.row_factory = sqlite3.Row
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def account_identity(
        self,
        provider: str,
        credential_sha256: str,
        initial: str,
        *,
        legacy_credential_sha256: str | None = None,
        known_credential_sha256: tuple[str, ...] = (),
    ) -> str:
        """Pin an account ledger across optional evidence changes and restarts."""
        from findb_fetcher.platform_limits import merge_identity

        return merge_identity(
            self,
            provider,
            credential_sha256,
            legacy_credential_sha256 or credential_sha256,
            initial,
            known_credential_sha256,
        )

    def reserve(
        self,
        *,
        account: str,
        window: str,
        requests: int,
        byte_count: int,
        request_limit: int,
        byte_limit: int,
        now: float,
    ) -> tuple[int, int]:
        if (
            any(
                type(value) is not int or value < 0
                for value in (requests, byte_count, request_limit, byte_limit)
            )
            or request_limit < 1
            or byte_limit < 1
        ):
            raise QuotaBlockedError("account limits are unknown or invalid")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_capacity(db, account)
            db.execute(
                "INSERT OR IGNORE INTO full_quota(account,window,requests,bytes) VALUES(?,?,0,0)",
                (account, window),
            )
            row = db.execute(
                "SELECT * FROM full_quota WHERE account=? AND window=?", (account, window)
            ).fetchone()
            if (
                row["blocked_until"] > now
                or row["requests"] + requests > request_limit
                or row["bytes"] + byte_count > byte_limit
            ):
                raise QuotaBlockedError("durable account quota is exhausted", quota_wait=True)
            before = row["requests"]
            db.execute(
                "UPDATE full_quota SET requests=requests+?, bytes=bytes+? WHERE account=? AND window=?",
                (requests, byte_count, account, window),
            )
            return before, before + requests

    def usage_floor(self, *, account: str, window: str, requests: int, byte_count: int) -> None:
        """Retain verified pre-install usage without resetting already recorded debt."""
        if (
            type(requests) is not int
            or type(byte_count) is not int
            or min(requests, byte_count) < 0
        ):
            raise QuotaBlockedError("verified prior usage invalid")
        with self.connection() as db:
            db.execute(
                "INSERT INTO full_quota(account,window,requests,bytes) VALUES(?,?,?,?) ON CONFLICT(account,window) DO UPDATE SET requests=MAX(requests,excluded.requests),bytes=MAX(bytes,excluded.bytes)",
                (account, window, requests, byte_count),
            )

    def reserve_acquisition(
        self,
        *,
        account: str,
        interval: float,
        requests: int,
        byte_count: int,
        request_limit: int | None,
        byte_limit: int | None,
        minute_limit: int | None,
        enforce_capacity: bool = True,
        allocation: str | None = None,
        allocation_request_limit: int = 0,
        allocation_byte_limit: int = 0,
    ) -> tuple[float, int, int, str]:
        """Atomically take an account pacing permit and daily/minute quota.

        A future UTC permit or rejected minute spends no quota.
        """
        if (
            not math.isfinite(interval)
            or interval < 0
            or any(type(value) is not int or value < 0 for value in (requests, byte_count))
            or any(
                value is not None and (type(value) is not int or value < 1)
                for value in (request_limit, byte_limit, minute_limit)
            )
        ):
            raise QuotaBlockedError("account limits are unknown or invalid")
        rejected = False
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if enforce_capacity:
                self._assert_capacity(db, account)
            # BEGIN IMMEDIATE may wait for another process. Sample the grant clock
            # only after owning the lock, for both pacing and quota boundaries.
            now = self.clock()
            if not math.isfinite(now):
                raise QuotaBlockedError("account clock is invalid")
            timestamp = datetime.fromtimestamp(now, timezone.utc)
            daily = timestamp.date().isoformat()
            minute = timestamp.strftime("%Y-%m-%dT%H:%M")
            permit = db.execute(
                "SELECT next_at FROM full_pacing WHERE account=?", (account,)
            ).fetchone()
            if permit is not None:
                if not math.isfinite(permit["next_at"]):
                    raise QuotaBlockedError("durable account pacing is invalid")
                if permit["next_at"] > now:
                    return permit["next_at"] - now, 0, 0, daily
            for window in (daily, minute):
                db.execute(
                    "INSERT OR IGNORE INTO full_quota(account,window,requests,bytes) VALUES(?,?,0,0)",
                    (account, window),
                )
            day = db.execute(
                "SELECT * FROM full_quota WHERE account=? AND window=?", (account, daily)
            ).fetchone()
            if (
                day["blocked_until"] > now
                or (request_limit is not None and day["requests"] + requests > request_limit)
                or (
                    byte_limit is not None
                    and (day["bytes"] >= byte_limit or day["bytes"] + byte_count > byte_limit)
                )
            ):
                raise QuotaBlockedError("durable account quota is exhausted", quota_wait=True)
            minute_row = db.execute(
                "SELECT * FROM full_quota WHERE account=? AND window=?", (account, minute)
            ).fetchone()
            if minute_row["blocked_until"] > now or (
                minute_limit is not None and minute_row["requests"] + requests > minute_limit
            ):
                raise QuotaBlockedError("durable account quota is exhausted", quota_wait=True)
            if allocation is not None:
                allocation_account = account + ":allocation:" + allocation
                db.execute(
                    "INSERT OR IGNORE INTO full_quota(account,window,requests,bytes) VALUES(?,?,0,0)",
                    (allocation_account, daily),
                )
                allocated = db.execute(
                    "SELECT requests,bytes FROM full_quota WHERE account=? AND window=?",
                    (allocation_account, daily),
                ).fetchone()
                if (
                    allocated["requests"] + requests > allocation_request_limit
                    or allocated["bytes"] + byte_count > allocation_byte_limit
                ):
                    raise QuotaBlockedError(
                        "installed consumer allocation is exhausted", quota_wait=True
                    )
                db.execute(
                    "UPDATE full_quota SET requests=requests+?,bytes=bytes+? WHERE account=? AND window=?",
                    (requests, byte_count, allocation_account, daily),
                )
            before = day["requests"]
            db.execute(
                "UPDATE full_quota SET requests=requests+?,bytes=bytes+? WHERE account=? AND window=?",
                (requests, byte_count, account, daily),
            )
            row = db.execute(
                "SELECT * FROM full_quota WHERE account=? AND window=?", (account, minute)
            ).fetchone()
            if row["blocked_until"] > now or (
                minute_limit is not None and row["requests"] + requests > minute_limit
            ):
                rejected = True
            else:
                db.execute(
                    "UPDATE full_quota SET requests=requests+? WHERE account=? AND window=?",
                    (requests, account, minute),
                )
                self.record_floor(db, account, now + interval, reason="pacing")
        if rejected:
            raise QuotaBlockedError("durable account quota is exhausted", quota_wait=True)
        return 0, before, before + requests, daily

    def pace_from_completion(self, *, account: str, now: float, interval: float) -> None:
        """Extend the account permit after a slow/failed SDK call, using UTC epoch."""
        with self.connection() as db:
            self.record_floor(db, account, now + interval, reason="pacing")

    def record_byte_overage(self, *, account: str, window: str, byte_count: int) -> None:
        """Keep already-observed overage even when it exceeds the approved quota."""
        if type(byte_count) is not int or byte_count < 0:
            raise QuotaBlockedError("observed account bytes are invalid")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE full_quota SET bytes=bytes+? WHERE account=? AND window=?",
                (byte_count, account, window),
            )

    @staticmethod
    def _assert_capacity(db: Any, account: str) -> None:
        if db.execute(
            "SELECT 1 FROM full_capacity_violation WHERE account=?", (account,)
        ).fetchone():
            raise QuotaBlockedError(
                "persistent account capacity violation; trusted correction required"
            )

    def assert_capacity(self, account: str) -> None:
        with self.connection() as db:
            self._assert_capacity(db, account)

    def observe_capacity(
        self,
        *,
        account: str,
        window: str,
        bound: int,
        observed: int,
        allocation_sha256: str,
        allocation: str | None = None,
        declared: int = 0,
    ) -> None:
        """Commit received-byte debt and account-wide blocking in one transaction."""
        import secrets

        if type(observed) is not int or observed < 0 or type(declared) is not int or declared < 0:
            raise QuotaBlockedError("observed account bytes are invalid")
        if max(observed, declared) <= bound:
            return
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for identity in (
                [account, account + ":allocation:" + allocation] if allocation else [account]
            ):
                db.execute(
                    "UPDATE full_quota SET bytes=bytes+? WHERE account=? AND window=?",
                    (max(0, observed - bound), identity, window),
                )
            db.execute(
                "INSERT INTO full_capacity_violation VALUES(?,?,?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET observed_bytes=MAX(observed_bytes,excluded.observed_bytes),received_bytes=MAX(received_bytes,excluded.received_bytes),declared_bytes=MAX(declared_bytes,excluded.declared_bytes),violation_id=excluded.violation_id,recorded_at=excluded.recorded_at",
                (
                    account,
                    secrets.token_hex(16),
                    max(observed, declared),
                    allocation_sha256,
                    self.clock(),
                    observed,
                    declared,
                ),
            )

    def repair_capacity(
        self,
        *,
        account: str,
        violation_id: str,
        corrected_bound: int,
        allocation_sha256: str,
        declaration_sha256: str,
    ) -> None:
        """Trusted command CAS; daily usage and pacing remain untouched."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM full_capacity_violation WHERE account=?", (account,)
            ).fetchone()
            if row is None or row["violation_id"] != violation_id:
                raise QuotaBlockedError("capacity violation changed; inspect current violation")
            if (
                corrected_bound < row["observed_bytes"]
                or allocation_sha256 == row["allocation_sha256"]
            ):
                raise QuotaBlockedError(
                    "corrected installed allocation does not cover observed capacity"
                )
            db.execute(
                "INSERT INTO full_capacity_repair VALUES(?,?,?,?,?,?,?)",
                (
                    violation_id,
                    account,
                    row["observed_bytes"],
                    row["allocation_sha256"],
                    allocation_sha256,
                    declaration_sha256,
                    self.clock(),
                ),
            )
            db.execute("DELETE FROM full_capacity_violation WHERE account=?", (account,))

    def rate_limited(self, *, account: str, window: str, until: float) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # A response can arrive on a new UTC day without a quota row there.
            # Account pacing retains the cooldown without reserving future quota.
            self.record_floor(db, account, until, reason="rate_limited")
            db.execute(
                "UPDATE full_quota SET blocked_until=MAX(blocked_until,?) WHERE account=? AND window=?",
                (until, account, window),
            )

    def record_plan(self, plan: dict[str, Any], provider: str) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO full_plan VALUES(?,?,?,?,?,0,0)",
                (
                    plan["plan_id"],
                    provider,
                    plan["dataset_key"],
                    plan["trade_date"],
                    canonical_bytes(plan),
                ),
            )
            db.execute("INSERT OR IGNORE INTO full_plan_reconcile VALUES(?,0)", (plan["plan_id"],))
            for part in plan["parts"]:
                for member in part["member_keys"]:
                    key = f"{part['work_item_id']}:{member}"
                    db.execute(
                        "INSERT OR IGNORE INTO full_work(key,provider,plan_id,member_key) VALUES(?,?,?,?)",
                        (key, provider, plan["plan_id"], member),
                    )

    def eligible(self, key: str, *, now: float) -> bool:
        """Avoid remote restore probes for terminal, leased or deferred members."""
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM full_work WHERE key=? AND status NOT IN ('complete','manual') AND lease_until<=? AND next_at<=?",
                    (key, now, now),
                ).fetchone()
                is not None
            )

    def claim(
        self,
        key: str,
        *,
        now: float,
        lease_seconds: float = 600,
        next_cursor: int | None = None,
    ) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,lease_until,next_at,plan_id FROM full_work WHERE key=?", (key,)
            ).fetchone()
            if (
                row is None
                or row["status"] in {"complete", "manual"}
                or row["lease_until"] > now
                or row["next_at"] > now
            ):
                return False
            db.execute(
                "UPDATE full_work SET lease_until=?, attempts=attempts+1 WHERE key=?",
                (now + lease_seconds, key),
            )
            if next_cursor is not None:
                db.execute(
                    "INSERT INTO full_schedule(scope,cursor) VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET cursor=excluded.cursor",
                    (row["plan_id"], next_cursor),
                )
            return True

    def schedule_cursor(self, scope: str) -> int:
        with self.connection() as db:
            row = db.execute("SELECT cursor FROM full_schedule WHERE scope=?", (scope,)).fetchone()
            return row[0] if row else 0

    def schedule_save(
        self, scope: str, *, cursor: int | None = None, batch: dict | None = None
    ) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO full_schedule(scope,cursor,batch) VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET cursor=COALESCE(?,cursor),batch=COALESCE(excluded.batch,batch)",
                (scope, cursor or 0, canonical_bytes(batch) if batch is not None else None, cursor),
            )

    def plan_progress(self, plan_id: str) -> dict[str, Any]:
        with self.connection() as db:
            counts = dict(
                db.execute(
                    "SELECT status,COUNT(*) FROM full_work WHERE plan_id=? GROUP BY status",
                    (plan_id,),
                ).fetchall()
            )
            failures = db.execute(
                "SELECT member_key,reason,attempts FROM full_work WHERE plan_id=? AND reason IN ('source_error','source_rejected','normalization_failed','mapping_gap','retry_exhausted') ORDER BY key",
                (plan_id,),
            ).fetchall()
            batch = db.execute(
                "SELECT batch FROM full_schedule WHERE scope=?", (plan_id,)
            ).fetchone()
        return {
            "counts": counts,
            "failures": [dict(row) for row in failures],
            "last_batch": json.loads(batch[0]) if batch and batch[0] else None,
        }

    def prepared(self, key: str) -> bytes | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT body,body_sha256 FROM full_work WHERE key=?", (key,)
            ).fetchone()
            if row is None or row["body"] is None:
                return None
            body = bytes(row["body"])
            if checksum(body) != row["body_sha256"]:
                raise FullMarketStateError("immutable prepared body checksum mismatch")
            return body

    def acquisition_members(self, plan_id: str, *, now: float) -> set[str]:
        """Only currently eligible work without a persisted provider response needs quota."""
        with self.connection() as db:
            rows = db.execute(
                "SELECT member_key FROM full_work WHERE plan_id=? AND status IN ('pending','prepared') AND body IS NULL AND lease_until<=? AND next_at<=?",
                (plan_id, now, now),
            ).fetchall()
            return {row["member_key"] for row in rows}

    def receipt(self, key: str) -> str | None:
        with self.connection() as db:
            row = db.execute("SELECT receipt FROM full_work WHERE key=?", (key,)).fetchone()
            return row["receipt"] if row is not None else None

    def sequence_receipt(self, key: str, idempotency_key: str) -> str | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT run_id FROM full_work_receipt WHERE key=? AND idempotency_key=?",
                (key, idempotency_key),
            ).fetchone()
            return row[0] if row else None

    def save_sequence_receipt(self, key: str, idempotency_key: str, run_id: str) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO full_work_receipt VALUES(?,?,?)",
                (key, idempotency_key, run_id),
            )
            row = db.execute(
                "SELECT run_id FROM full_work_receipt WHERE key=? AND idempotency_key=?",
                (key, idempotency_key),
            ).fetchone()
            if row[0] != run_id:
                raise FullMarketStateError("sequence receipt identity changed")

    def prepare(self, key: str, body: bytes) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM full_work WHERE key=?", (key,)).fetchone()
            if row is None or (row["body"] is not None and bytes(row["body"]) != body):
                raise FullMarketStateError("prepared body cannot be changed")
            db.execute(
                "UPDATE full_work SET body=?,body_sha256=?,status='prepared' WHERE key=?",
                (body, checksum(body), key),
            )

    def defer_wait(
        self,
        key: str,
        *,
        now: float,
        reason: str = "rate_limited",
        delay_seconds: int = 60,
    ) -> None:
        """Authorized waiting retains prepared bytes and the original failure budget."""
        if delay_seconds < 1:
            raise FullMarketStateError("wait delay must be positive")
        with self.connection() as db:
            db.execute(
                "UPDATE full_work SET status=CASE WHEN body IS NULL THEN 'pending' ELSE 'prepared' END,reason=?,lease_until=0,next_at=?,attempts=MAX(0,attempts-1) WHERE key=?",
                (reason, now + delay_seconds, key),
            )

    def pending_source_error_keys(self, plan_id: str) -> set[str]:
        with self.connection() as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT key FROM full_work WHERE plan_id=? AND reason='source_error_pending'",
                    (plan_id,),
                )
            }

    def stage_source_error(self, key: str) -> None:
        """Retain a report continuation without spending or refunding attempts.

        Even the last acquisition attempt must report before terminalization.
        Prepared bytes and receipts stay untouched while Source is unavailable.
        """
        with self.connection() as db:
            db.execute(
                "UPDATE full_work SET status=CASE WHEN body IS NULL THEN 'pending' ELSE 'prepared' END,reason='source_error_pending',lease_until=0,next_at=0 WHERE key=?",
                (key,),
            )
            db.execute(
                "INSERT INTO full_plan_reconcile SELECT plan_id,1 FROM full_work WHERE key=? ON CONFLICT(plan_id) DO UPDATE SET dirty=1",
                (key,),
            )

    def finish(
        self,
        key: str,
        *,
        status: str,
        reason: str | None = None,
        receipt: str | None = None,
        now: float = 0,
    ) -> None:
        if status not in {"pending", "prepared", "complete", "manual"}:
            raise FullMarketStateError("work status is invalid")
        with self.connection() as db:
            if reason == "normalizing":
                db.execute("UPDATE full_work SET attempts=MAX(0,attempts-1) WHERE key=?", (key,))
            db.execute(
                "UPDATE full_work SET status=?,reason=?,receipt=COALESCE(?,receipt),lease_until=0,next_at=? WHERE key=?",
                (
                    status,
                    reason,
                    receipt,
                    now + 60 if status in {"pending", "prepared"} else 0,
                    key,
                ),
            )
            db.execute(
                "UPDATE full_work SET status='manual',reason='retry_exhausted' WHERE key=? AND attempts>=? AND status IN ('pending','prepared') AND reason != 'normalizing'",
                (key, FULL_WORK_MAX_ATTEMPTS),
            )
            # A crash after terminal local progress must not lose the final
            # Source evaluation just because no prepared member remains.
            db.execute(
                "INSERT INTO full_plan_reconcile SELECT plan_id,1 FROM full_work WHERE key=? ON CONFLICT(plan_id) DO UPDATE SET dirty=1",
                (key,),
            )

    def catchup_dates(
        self,
        provider: str,
        dataset: str,
        *,
        activation: date,
        through: date,
        open_dates: list[date],
    ) -> list[date]:
        # The cursor records planning, not completion; existing unresolved plans are revisited.
        return [day for day in open_dates if activation <= day <= through]

    def pending_plans(self, provider: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM full_plan WHERE provider=? AND complete=0 ORDER BY trade_date",
                    (provider,),
                )
            ]

    def drain_plans(self, provider: str, *, now: float) -> list[dict[str, Any]]:
        """Select only eligible immutable deliveries, without loading historical bodies."""
        with self.connection() as db:
            keys: dict[str, set[str]] = {}
            # Keep the idle lookup proportional to prepared work. Otherwise the
            # planner may prefer provider/status and scan a large pending backlog.
            for row in db.execute(
                "SELECT plan_id,key FROM full_work INDEXED BY full_work_prepared_drain WHERE provider=? AND body IS NOT NULL AND status IN ('pending','prepared') AND lease_until<=? AND next_at<=?",
                (provider, now, now),
            ):
                keys.setdefault(row["plan_id"], set()).add(row["key"])
            for row in db.execute(
                "SELECT p.plan_id FROM full_plan p JOIN full_plan_reconcile r ON r.plan_id=p.plan_id WHERE p.provider=? AND p.complete=0 AND r.dirty=1",
                (provider,),
            ):
                keys.setdefault(row["plan_id"], set())
            plans = []
            for plan_id, selected in keys.items():
                saved = db.execute(
                    "SELECT * FROM full_plan WHERE plan_id=? AND provider=? AND complete=0",
                    (plan_id, provider),
                ).fetchone()
                if saved is not None:
                    plans.append({**dict(saved), "drain_keys": selected})
            return sorted(plans, key=lambda plan: (plan["trade_date"], plan["dataset"]))

    def evaluate(self, plan: dict[str, Any]) -> None:
        summary = plan["summary"]
        complete = (
            summary["expected"] == summary["data"] + summary["no_data"]
            and summary["missing"] == summary["blocked"] == 0
        )
        with self.connection() as db:
            db.execute(
                "UPDATE full_plan SET complete=?,late=? WHERE plan_id=?",
                (int(complete), int(summary["is_late"]), plan["plan_id"]),
            )
            db.execute(
                "INSERT INTO full_plan_reconcile VALUES(?,0) ON CONFLICT(plan_id) DO UPDATE SET dirty=0",
                (plan["plan_id"],),
            )
            if complete:
                db.execute(
                    "UPDATE full_work SET status='complete',lease_until=0 WHERE plan_id=?",
                    (plan["plan_id"],),
                )

    def refresh_due(self, provider: str, now: float) -> bool:
        with self.connection() as db:
            row = db.execute(
                "SELECT next_at FROM full_refresh WHERE provider=?", (provider,)
            ).fetchone()
            return row is None or row["next_at"] <= now

    def refresh(
        self,
        provider: str,
        *,
        now: float,
        result: list[dict[str, Any]],
        next_at: float | None = None,
    ) -> None:
        delay = 3600 if any(item.get("status") == "blocked" for item in result) else 86400
        with self.connection() as db:
            db.execute(
                "INSERT INTO full_refresh VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET next_at=excluded.next_at,result=excluded.result",
                (
                    provider,
                    next_at if next_at is not None else now + delay,
                    canonical_bytes(result),
                ),
            )

    def date_prerequisite_due(self, provider: str, dataset: str, day: date) -> bool:
        with self.connection() as db:
            row = db.execute(
                "SELECT next_at FROM full_date_prerequisite WHERE provider=? AND dataset=? AND trade_date=?",
                (provider, dataset, day.isoformat()),
            ).fetchone()
        return row is None or row[0] <= self.clock()

    def date_prerequisite_delay(self, provider: str, dataset: str, day: date) -> int:
        with self.connection() as db:
            row = db.execute(
                "SELECT next_at FROM full_date_prerequisite WHERE provider=? AND dataset=? AND trade_date=?",
                (provider, dataset, day.isoformat()),
            ).fetchone()
        if row is None:
            return 0
        return max(0, math.ceil(row[0] - self.clock()))

    def block_date(self, provider: str, dataset: str, day: date, reason: str) -> None:
        if reason not in {"dated_report_unavailable", "dated_universe_pending"}:
            raise FullMarketStateError("unknown dated prerequisite reason")
        with self.connection() as db:
            db.execute(
                "INSERT INTO full_date_prerequisite VALUES(?,?,?,?,?) ON CONFLICT(provider,dataset,trade_date) DO UPDATE SET reason=excluded.reason,next_at=excluded.next_at",
                (provider, dataset, day.isoformat(), reason, self.clock() + 3600),
            )

    def clear_date(self, provider: str, dataset: str, day: date) -> None:
        with self.connection() as db:
            db.execute(
                "DELETE FROM full_date_prerequisite WHERE provider=? AND dataset=? AND trade_date=?",
                (provider, dataset, day.isoformat()),
            )

    def health(self, provider: str) -> dict[str, Any]:
        with self.connection() as db:
            gaps = [
                dict(row)
                for row in db.execute(
                    "SELECT plan_id,dataset AS dataset_key,trade_date,late FROM full_plan WHERE provider=? AND complete=0 ORDER BY trade_date",
                    (provider,),
                )
            ]
            manual = db.execute(
                "SELECT COUNT(*) FROM full_work WHERE provider=? AND status='manual'", (provider,)
            ).fetchone()[0]
            refresh = db.execute(
                "SELECT result FROM full_refresh WHERE provider=?", (provider,)
            ).fetchone()
            prerequisites = [
                dict(row)
                for row in db.execute(
                    "SELECT dataset,trade_date,reason,next_at FROM full_date_prerequisite WHERE provider=? ORDER BY trade_date",
                    (provider,),
                )
            ]
        return {
            "acquisition_protocol_version": 2,
            "universe_refresh": json.loads(refresh["result"]) if refresh else [],
            "provider": provider,
            "unresolved": gaps,
            "manual_required": manual,
            "date_prerequisites": prerequisites,
            "plans": [{**gap, **self.plan_progress(gap["plan_id"])} for gap in gaps],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
