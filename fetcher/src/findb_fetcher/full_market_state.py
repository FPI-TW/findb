"""Shared SQLite reservations, work leases and immutable full-market prepared bodies."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from findb_fetcher.full_market_universe import canonical_bytes, checksum

_DIAGNOSTIC_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{0,39}(:[A-Za-z][A-Za-z0-9_]{0,59})?$")
# Members other than no-data keep their own next_at; a no-data member waits for the plan's
# retry round and is fetched at most once in it. Callers join full_no_data_round as r.
_ELIGIBLE_WORK = (
    "w.status NOT IN ('complete','manual') AND w.lease_until<=:now AND w.next_at<=:now "
    "AND (COALESCE(w.reason,'')!='no_data' "
    "OR (w.attempts<COALESCE(r.round,1) AND COALESCE(r.not_before,0)<=:now))"
)

FULL_WORK_MAX_ATTEMPTS = 5
# Consecutive no-data members that stop a round: past this it is unlikely to be per symbol.
# Also the acquired members that prove the date is open and the provider is serving it.
FULL_WORK_NO_DATA_BREAKER = 20
# A symbol's end-of-day row can publish hours later than the rest, so its retries span hours.
# One wait after each completed retry round but the last; FULL_WORK_MAX_ATTEMPTS includes the
# first fetch. A round ends once every no-data member has been fetched in it.
FULL_WORK_NO_DATA_RETRY_DELAYS = (1800, 3600, 3600, 7200)
FULL_WORK_NO_DATA_BREAKER_HOLD_SECONDS = 1800
# Settled intents and past quota windows outlive every liability, login and cooldown read.
ACCOUNT_LEDGER_RETENTION_SECONDS = 7 * 86400
ACCOUNT_LEDGER_PRUNE_INTERVAL_SECONDS = 86400
ACCOUNT_LEDGER_PRUNE_BATCH = 5000


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
            # Index builds sort the whole table; consumers run with a read-only
            # rootfs and a 16 MiB /tmp, so a spill there fails as "disk is full".
            db.execute("PRAGMA temp_store=MEMORY")
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
                CREATE TABLE IF NOT EXISTS full_member_repair (repair_id TEXT PRIMARY KEY, work_key TEXT NOT NULL, original_attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS full_empty_outcome (work_key TEXT PRIMARY KEY, exhausted INTEGER NOT NULL, next_at REAL NOT NULL, reason TEXT NOT NULL DEFAULT 'empty_response');
                CREATE INDEX IF NOT EXISTS full_plan_reconcile_dirty ON full_plan_reconcile(dirty,plan_id);
                INSERT OR IGNORE INTO full_plan_reconcile SELECT plan_id,1 FROM full_plan WHERE complete=0;
                CREATE TABLE IF NOT EXISTS full_work_receipt (key TEXT NOT NULL, idempotency_key TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(key,idempotency_key));
                CREATE TABLE IF NOT EXISTS full_work_diagnostic (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, provider TEXT NOT NULL, category TEXT NOT NULL, http_status INTEGER, provider_code INTEGER, dispatched INTEGER, observed_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS full_work_diagnostic_key ON full_work_diagnostic(key,id);
                CREATE TABLE IF NOT EXISTS full_refresh (provider TEXT PRIMARY KEY, next_at REAL NOT NULL, result BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS full_date_prerequisite (provider TEXT NOT NULL, dataset TEXT NOT NULL, trade_date TEXT NOT NULL, reason TEXT NOT NULL, next_at REAL NOT NULL, PRIMARY KEY(provider,dataset,trade_date));
                CREATE TABLE IF NOT EXISTS full_provider_outage (plan_id TEXT PRIMARY KEY, since REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS full_no_data_trip (plan_id TEXT PRIMARY KEY, trips INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS full_no_data_round (plan_id TEXT PRIMARY KEY, round INTEGER NOT NULL, not_before REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS full_cursor (provider TEXT, dataset TEXT, trade_date TEXT NOT NULL, PRIMARY KEY(provider,dataset));
                CREATE TABLE IF NOT EXISTS full_schedule (scope TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0, batch BLOB);
                CREATE TABLE IF NOT EXISTS acquisition_intent(operation_id TEXT PRIMARY KEY, account TEXT NOT NULL, provider TEXT NOT NULL, armed_at REAL NOT NULL, status TEXT NOT NULL, cooldown_bound REAL NOT NULL, actual_start REAL, completed_at REAL);
                CREATE TABLE IF NOT EXISTS acquisition_usage(operation_id TEXT NOT NULL, observed_at REAL NOT NULL, bytes INTEGER NOT NULL, limit_bytes INTEGER NOT NULL, remaining_bytes INTEGER NOT NULL, PRIMARY KEY(operation_id,observed_at));
                CREATE TABLE IF NOT EXISTS account_alias_audit(alias TEXT PRIMARY KEY, account TEXT NOT NULL, original_quota TEXT NOT NULL, original_pacing REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS account_ledger_retention (id INTEGER PRIMARY KEY CHECK(id=1), next_at REAL NOT NULL);
            """)
            if "reason" not in {
                row[1] for row in db.execute("PRAGMA table_info(full_empty_outcome)")
            }:
                db.execute(
                    "ALTER TABLE full_empty_outcome ADD COLUMN reason TEXT NOT NULL DEFAULT 'empty_response'"
                )
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
            # Armed recovery and unknown liability run on every request; an
            # account-prefix scan grows with the whole settled history.
            db.execute(
                "CREATE INDEX IF NOT EXISTS acquisition_intent_status ON acquisition_intent(account,status,completed_at)"
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

    def prune_account_ledger(
        self, *, batch: int = ACCOUNT_LEDGER_PRUNE_BATCH
    ) -> dict[str, int] | None:
        """Drop settled account history past retention, at most once a day per ledger.

        Recovery reads armed intents, liability reads unknown ones and quota reads
        current windows, so finalized/cancelled history and past windows count
        nowhere. Open debt is never pruned and usage is never refunded. Shioaji
        history stays as evidence for its retirement tooling.
        """
        now = self.clock()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT next_at FROM account_ledger_retention WHERE id=1").fetchone()
            if row is not None and now < row[0]:
                return None
            # Claim before deleting so concurrent consumers skip; a crash resumes tomorrow.
            db.execute(
                "INSERT INTO account_ledger_retention VALUES(1,?) ON CONFLICT(id) DO UPDATE SET next_at=excluded.next_at",
                (now + ACCOUNT_LEDGER_PRUNE_INTERVAL_SECONDS,),
            )
        cutoff = now - ACCOUNT_LEDGER_RETENTION_SECONDS
        cutoff_day = datetime.fromtimestamp(cutoff, timezone.utc).date().isoformat()
        with self.connection() as db:
            retained = {
                str(row[0])
                for row in db.execute(
                    "SELECT account FROM full_account_identity WHERE provider='shioaji' "
                    "UNION SELECT account FROM acquisition_intent WHERE provider='shioaji'"
                )
            }
            intent_accounts = [
                str(row[0]) for row in db.execute("SELECT DISTINCT account FROM acquisition_intent")
            ]
            quota_accounts = [
                str(row[0]) for row in db.execute("SELECT DISTINCT account FROM full_quota")
            ]
        removed = {"intents": 0, "quota_windows": 0}
        for account in intent_accounts:
            if account in retained:
                continue
            for status in ("finalized", "cancelled"):
                while True:
                    with self.connection() as db:
                        db.execute("BEGIN IMMEDIATE")
                        ids = [
                            (row[0],)
                            for row in db.execute(
                                "SELECT operation_id FROM acquisition_intent WHERE account=? AND status=? AND completed_at<? LIMIT ?",
                                (account, status, cutoff, batch),
                            )
                        ]
                        db.executemany("DELETE FROM acquisition_usage WHERE operation_id=?", ids)
                        db.executemany("DELETE FROM acquisition_intent WHERE operation_id=?", ids)
                    removed["intents"] += len(ids)
                    if len(ids) < batch:
                        break
        for account in quota_accounts:
            if account in retained:
                continue
            while True:
                with self.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    # Day and minute windows both sort by their leading ISO date.
                    windows = [
                        (account, row[0])
                        for row in db.execute(
                            "SELECT window FROM full_quota WHERE account=? AND window<? AND blocked_until<? LIMIT ?",
                            (account, cutoff_day, now, batch),
                        )
                    ]
                    db.executemany("DELETE FROM full_quota WHERE account=? AND window=?", windows)
                removed["quota_windows"] += len(windows)
                if len(windows) < batch:
                    break
        return removed

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
                    "SELECT 1 FROM full_work w LEFT JOIN full_no_data_round r ON r.plan_id=w.plan_id "
                    f"WHERE w.key=:key AND {_ELIGIBLE_WORK}",
                    {"key": key, "now": now},
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
                "SELECT w.plan_id FROM full_work w LEFT JOIN full_no_data_round r ON r.plan_id=w.plan_id "
                f"WHERE w.key=:key AND {_ELIGIBLE_WORK}",
                {"key": key, "now": now},
            ).fetchone()
            if row is None:
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
                "SELECT member_key,reason,attempts FROM full_work WHERE plan_id=? AND reason IN ('source_error','source_rejected','normalization_failed','mapping_gap','retry_exhausted','empty_response','unknown_dispatch') ORDER BY key",
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
                "SELECT w.member_key FROM full_work w LEFT JOIN full_no_data_round r ON r.plan_id=w.plan_id "
                f"WHERE w.plan_id=:plan_id AND w.status IN ('pending','prepared') AND w.body IS NULL AND {_ELIGIBLE_WORK}",
                {"plan_id": plan_id, "now": now},
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
        """Authorized waiting retains prepared bytes and the original failure budget.

        A no-data member keeps its reason, so it stays unfetched in the current retry round.
        """
        if delay_seconds < 1:
            raise FullMarketStateError("wait delay must be positive")
        with self.connection() as db:
            db.execute(
                "UPDATE full_work SET status=CASE WHEN body IS NULL THEN 'pending' ELSE 'prepared' END,reason=CASE WHEN reason='no_data' THEN reason ELSE ? END,lease_until=0,next_at=?,attempts=MAX(0,attempts-1) WHERE key=?",
                (reason, now + delay_seconds, key),
            )

    def defer_provider_outage(
        self, key: str, *, now: float, delay_seconds: int, limit_seconds: float
    ) -> bool:
        """Defer a shared-report outage without charging the member.

        The plan's continuous outage starts at its first deferral; once it
        reaches ``limit_seconds`` the caller must stop retrying the member.
        """
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO full_provider_outage SELECT plan_id,? FROM full_work WHERE key=?",
                (now, key),
            )
            row = db.execute(
                "SELECT o.since FROM full_provider_outage o JOIN full_work w ON w.plan_id=o.plan_id WHERE w.key=?",
                (key,),
            ).fetchone()
        if row is None or now - float(row[0]) >= limit_seconds:
            return False
        self.defer_wait(key, now=now, reason="provider_unavailable", delay_seconds=delay_seconds)
        return True

    def clear_provider_outage(self, plan_id: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM full_provider_outage WHERE plan_id=?", (plan_id,))

    def has_blocking_manual(self, plan_id: str) -> bool:
        """Whether a manual member needs review before the rest of the plan may continue.

        A mapping gap or a symbol out of no-data retries concerns only that member,
        and so does any other no-data reply until the breaker keeps tripping with no
        data in between. Any other manual reason may be systemic, so it still ends the
        plan at once.
        """
        with self.connection() as db:
            manual = db.execute(
                "SELECT 1 FROM full_work WHERE plan_id=? AND status='manual' "
                "AND COALESCE(reason,'') NOT IN ('mapping_gap','no_data_exhausted') LIMIT 1",
                (plan_id,),
            ).fetchone()
            trips = db.execute(
                "SELECT trips FROM full_no_data_trip WHERE plan_id=?", (plan_id,)
            ).fetchone()
        return manual is not None or (trips is not None and trips[0] >= FULL_WORK_MAX_ATTEMPTS)

    def counts_toward_no_data_breaker(self, key: str) -> bool:
        """Whether this no-data reply can still point at the provider rather than the symbol.

        Retry rounds hold mostly members that already returned no data, so an unbroken
        run there is expected once the plan has proven the date is open and served.
        """
        with self.connection() as db:
            row = db.execute(
                "SELECT w.attempts,("
                "SELECT COUNT(*) FROM full_work p WHERE p.plan_id=w.plan_id "
                "AND p.status IN ('complete','prepared')"
                ") FROM full_work w WHERE w.key=?",
                (key,),
            ).fetchone()
        return row[0] <= 1 or row[1] < FULL_WORK_NO_DATA_BREAKER

    def record_no_data_trip(self, plan_id: str, *, until: float) -> None:
        """Hold members awaiting provider I/O; prepared bytes keep draining."""
        with self.connection() as db:
            db.execute(
                "INSERT INTO full_no_data_trip VALUES(?,1) "
                "ON CONFLICT(plan_id) DO UPDATE SET trips=trips+1",
                (plan_id,),
            )
            db.execute(
                "UPDATE full_work SET next_at=MAX(next_at,?) WHERE plan_id=? AND status='pending'",
                (until, plan_id),
            )

    def clear_no_data_trips(self, plan_id: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM full_no_data_trip WHERE plan_id=?", (plan_id,))

    def advance_no_data_round(self, plan_id: str, *, now: float) -> None:
        """Start the next no-data retry round once the current one has fetched every member.

        Round 1 is the first fetch of every member; each later round fetches every member
        still without data once. An interrupted round resumes with its unfetched members.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT round FROM full_no_data_round WHERE plan_id=?", (plan_id,)
            ).fetchone()
            unfetched = db.execute(
                "SELECT 1 FROM full_work WHERE plan_id=? AND status NOT IN ('complete','manual') "
                "AND attempts<? AND (reason='no_data' OR (attempts=0 AND body IS NULL)) LIMIT 1",
                (plan_id, current[0] if current else 1),
            ).fetchone()
            if unfetched is not None:
                return
            fetched = db.execute(
                "SELECT MIN(attempts) FROM full_work WHERE plan_id=? "
                "AND status NOT IN ('complete','manual') AND reason='no_data'",
                (plan_id,),
            ).fetchone()[0]
            if fetched is None:
                return
            delay = FULL_WORK_NO_DATA_RETRY_DELAYS[
                min(fetched, len(FULL_WORK_NO_DATA_RETRY_DELAYS)) - 1
            ]
            db.execute(
                "INSERT INTO full_no_data_round VALUES(?,?,?) ON CONFLICT(plan_id) "
                "DO UPDATE SET round=excluded.round,not_before=excluded.not_before",
                (plan_id, fetched + 1, now + delay),
            )

    def no_data_retry_at(self, plan_id: str) -> float | None:
        """Earliest next action of a plan with members awaiting a no-data retry round, if any."""
        with self.connection() as db:
            row = db.execute(
                "SELECT MIN(CASE WHEN COALESCE(w.reason,'')!='no_data' THEN w.next_at "
                "WHEN w.attempts<COALESCE(r.round,1) THEN MAX(w.next_at,COALESCE(r.not_before,0)) END),"
                "COALESCE(SUM(w.reason='no_data'),0) "
                "FROM full_work w LEFT JOIN full_no_data_round r ON r.plan_id=w.plan_id "
                "WHERE w.plan_id=? AND w.status NOT IN ('complete','manual')",
                (plan_id,),
            ).fetchone()
        return row[0] if row[1] else None

    def pending_source_error_keys(self, plan_id: str) -> set[str]:
        with self.connection() as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT key FROM full_work WHERE plan_id=? AND reason='source_error_pending'",
                    (plan_id,),
                )
            }

    def record_diagnostic(
        self,
        key: str,
        *,
        provider: str,
        category: str,
        http_status: int | None = None,
        provider_code: int | None = None,
        dispatched: bool | None = None,
        now: float | None = None,
    ) -> None:
        """Append one bounded failure class; free-text provider messages are never kept."""
        if not _DIAGNOSTIC_CATEGORY.fullmatch(category):
            raise FullMarketStateError("diagnostic category is invalid")
        codes = []
        for code in (http_status, provider_code):
            if code is not None and (type(code) is not int or not 0 <= code <= 999):
                code = None
            codes.append(code)
        with self.connection() as db:
            db.execute(
                "INSERT INTO full_work_diagnostic(key,provider,category,http_status,provider_code,dispatched,observed_at) VALUES(?,?,?,?,?,?,?)",
                (
                    key,
                    provider,
                    category,
                    *codes,
                    None if dispatched is None else int(dispatched),
                    self.clock() if now is None else now,
                ),
            )

    def diagnostics(self, key: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT category,http_status,provider_code,dispatched,observed_at FROM full_work_diagnostic WHERE key=? ORDER BY id",
                (key,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def member_complete(self, key: str) -> bool:
        with self.connection() as db:
            row = db.execute("SELECT status FROM full_work WHERE key=?", (key,)).fetchone()
            return row is not None and row[0] == "complete"

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
            # The plan's no-data retry round, not the member, decides when it is fetched again.
            retry_delay = 0 if reason == "no_data" else 60
            db.execute(
                "UPDATE full_work SET status=?,reason=?,receipt=COALESCE(?,receipt),lease_until=0,next_at=? WHERE key=?",
                (
                    status,
                    reason,
                    receipt,
                    now + retry_delay if status in {"pending", "prepared"} else 0,
                    key,
                ),
            )
            db.execute(
                "UPDATE full_work SET status='manual',reason=CASE WHEN reason='no_data' THEN 'no_data_exhausted' ELSE 'retry_exhausted' END WHERE key=? AND attempts>=? AND status IN ('pending','prepared') AND reason != 'normalizing'",
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

    def record_dated_release(self, provider: str, *, now: float, release: dict[str, Any]) -> None:
        """Report the newest dated release where cycles skip catalogue refresh."""
        dataset = release["dataset_key"]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT next_at,result FROM full_refresh WHERE provider=?", (provider,)
            ).fetchone()
            current = json.loads(row["result"]) if row else []
            # Profiles and late dates submit out of order; never regress to an older date.
            if any(
                item.get("dataset_key") == dataset
                and str(item.get("effective_date", "")) > release["effective_date"]
                for item in current
            ):
                return
            result = [item for item in current if item.get("dataset_key") != dataset]
            db.execute(
                "INSERT INTO full_refresh VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET result=excluded.result",
                (
                    provider,
                    row["next_at"] if row else now + 86400,
                    canonical_bytes([*result, release]),
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
            outages = [
                dict(row)
                for row in db.execute(
                    "SELECT o.plan_id,p.trade_date,o.since FROM full_provider_outage o JOIN full_plan p ON p.plan_id=o.plan_id WHERE p.provider=? ORDER BY p.trade_date",
                    (provider,),
                )
            ]
            # Latest failure class of each unfinished member, for operator triage.
            diagnostics = dict(
                db.execute(
                    "SELECT d.category,COUNT(*) FROM full_work_diagnostic d JOIN full_work w ON w.key=d.key "
                    "WHERE w.provider=? AND w.status!='complete' AND d.id=(SELECT MAX(id) FROM full_work_diagnostic WHERE key=d.key) "
                    "GROUP BY d.category ORDER BY d.category",
                    (provider,),
                ).fetchall()
            )
        return {
            "acquisition_protocol_version": 2,
            "universe_refresh": json.loads(refresh["result"]) if refresh else [],
            "provider": provider,
            "unresolved": gaps,
            "manual_required": manual,
            "date_prerequisites": prerequisites,
            "provider_outages": outages,
            "source_error_diagnostics": diagnostics,
            "plans": [{**gap, **self.plan_progress(gap["plan_id"])} for gap in gaps],
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
