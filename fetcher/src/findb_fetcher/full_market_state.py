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


class FullMarketStateError(RuntimeError):
    pass


class QuotaBlockedError(FullMarketStateError):
    pass


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
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS full_quota (account TEXT, window TEXT, requests INTEGER NOT NULL, bytes INTEGER NOT NULL, blocked_until REAL NOT NULL DEFAULT 0, PRIMARY KEY(account,window));
                CREATE TABLE IF NOT EXISTS full_pacing (account TEXT PRIMARY KEY, next_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS full_capacity_violation (account TEXT PRIMARY KEY, violation_id TEXT NOT NULL, observed_bytes INTEGER NOT NULL, allocation_sha256 TEXT NOT NULL, recorded_at REAL NOT NULL, received_bytes INTEGER NOT NULL DEFAULT 0, declared_bytes INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS full_capacity_repair (violation_id TEXT PRIMARY KEY, account TEXT NOT NULL, observed_bytes INTEGER NOT NULL, previous_allocation_sha256 TEXT NOT NULL, corrected_allocation_sha256 TEXT NOT NULL, declaration_sha256 TEXT NOT NULL, repaired_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS full_work (key TEXT PRIMARY KEY, provider TEXT NOT NULL, plan_id TEXT NOT NULL, member_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0, body BLOB, body_sha256 TEXT, receipt TEXT, reason TEXT, next_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS full_plan (plan_id TEXT PRIMARY KEY, provider TEXT NOT NULL, dataset TEXT NOT NULL, trade_date TEXT NOT NULL, body BLOB NOT NULL, complete INTEGER NOT NULL DEFAULT 0, late INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS full_refresh (provider TEXT PRIMARY KEY, next_at REAL NOT NULL, result BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS full_cursor (provider TEXT, dataset TEXT, trade_date TEXT NOT NULL, PRIMARY KEY(provider,dataset));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(full_capacity_violation)")}
            for column in ("received_bytes", "declared_bytes"):
                if column not in columns:
                    db.execute(
                        f"ALTER TABLE full_capacity_violation ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
            # Promote cooldowns written by older runtimes out of dated quota rows.
            db.execute(
                "INSERT INTO full_pacing(account,next_at) SELECT account,MAX(blocked_until) FROM full_quota WHERE blocked_until>0 GROUP BY account ON CONFLICT(account) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)"
            )

    @contextmanager
    def connection(self) -> Any:
        db = sqlite3.connect(self.path, timeout=30)
        try:
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
                raise QuotaBlockedError("durable account quota is exhausted")
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
        request_limit: int,
        byte_limit: int,
        minute_limit: int,
        allocation: str | None = None,
        allocation_request_limit: int = 0,
        allocation_byte_limit: int = 0,
    ) -> tuple[float, int, int, str]:
        """Atomically take an account pacing permit and daily/minute quota.

        A future UTC permit returns a wait without spending quota. Minute rejection
        commits the conservative daily reservation, but never grants a permit.
        """
        if (
            not math.isfinite(interval)
            or interval <= 0
            or any(
                type(value) is not int or value < 0
                for value in (requests, byte_count, request_limit, byte_limit, minute_limit)
            )
            or min(request_limit, byte_limit, minute_limit) < 1
        ):
            raise QuotaBlockedError("account limits are unknown or invalid")
        rejected = False
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
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
                or day["requests"] + requests > request_limit
                or day["bytes"] + byte_count > byte_limit
            ):
                raise QuotaBlockedError("durable account quota is exhausted")
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
                    raise QuotaBlockedError("installed consumer allocation is exhausted")
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
            if row["blocked_until"] > now or row["requests"] + requests > minute_limit:
                rejected = True
            else:
                db.execute(
                    "UPDATE full_quota SET requests=requests+? WHERE account=? AND window=?",
                    (requests, account, minute),
                )
                db.execute(
                    "INSERT INTO full_pacing VALUES(?,?) ON CONFLICT(account) DO UPDATE SET next_at=excluded.next_at",
                    (account, now + interval),
                )
        if rejected:
            raise QuotaBlockedError("durable account quota is exhausted")
        return 0, before, before + requests, daily

    def pace_from_completion(self, *, account: str, now: float, interval: float) -> None:
        """Extend the account permit after a slow/failed SDK call, using UTC epoch."""
        with self.connection() as db:
            db.execute(
                "INSERT INTO full_pacing VALUES(?,?) ON CONFLICT(account) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)",
                (account, now + interval),
            )

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
            db.execute(
                "INSERT INTO full_pacing VALUES(?,?) ON CONFLICT(account) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)",
                (account, until),
            )
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
            for part in plan["parts"]:
                for member in part["member_keys"]:
                    key = f"{part['work_item_id']}:{member}"
                    db.execute(
                        "INSERT OR IGNORE INTO full_work(key,provider,plan_id,member_key) VALUES(?,?,?,?)",
                        (key, provider, plan["plan_id"], member),
                    )

    def claim(self, key: str, *, now: float, lease_seconds: float = 600) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,lease_until,next_at FROM full_work WHERE key=?", (key,)
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
            return True

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
                "UPDATE full_work SET status='manual',reason='retry_exhausted' WHERE key=? AND attempts>=8 AND status IN ('pending','prepared') AND reason != 'normalizing'",
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

    def health(self, provider: str) -> dict[str, Any]:
        with self.connection() as db:
            gaps = [
                dict(row)
                for row in db.execute(
                    "SELECT plan_id,trade_date,late FROM full_plan WHERE provider=? AND complete=0 ORDER BY trade_date",
                    (provider,),
                )
            ]
            manual = db.execute(
                "SELECT COUNT(*) FROM full_work WHERE provider=? AND status='manual'", (provider,)
            ).fetchone()[0]
            refresh = db.execute(
                "SELECT result FROM full_refresh WHERE provider=?", (provider,)
            ).fetchone()
        return {
            "universe_refresh": json.loads(refresh["result"]) if refresh else [],
            "provider": provider,
            "unresolved": gaps,
            "manual_required": manual,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
