"""Actual-invocation governor. Network calls never hold a SQLite transaction."""

from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.platform_limits import account_identity, interval, limits

PROTOCOL_VERSION = 2
LOCAL_DISPATCH_SECONDS = 0.050
REMOTE_AGE_SECONDS = 5.0
SDK_SAMPLE_SECONDS = 5.0
_mutex_guard = threading.Lock()
_mutexes: dict[str, threading.RLock] = {}
_held_locks = threading.local()
_policy: ContextVar[Callable[[], Authorization] | None] = ContextVar(
    "acquisition_policy", default=None
)
_state_path: ContextVar[Path | None] = ContextVar("acquisition_state_path", default=None)


@dataclass(frozen=True)
class Authorization:
    origin: float
    receipt_utc: float
    checked_at: float
    expires_at: float | None = None
    provider: str | None = None

    def deadline(self) -> float:
        remaining = REMOTE_AGE_SECONDS - (self.receipt_utc - self.checked_at)
        if self.expires_at is not None:
            remaining = min(remaining, self.expires_at - self.receipt_utc)
        minute_remaining = 60 - self.receipt_utc % 60
        return self.origin + min(LOCAL_DISPATCH_SECONDS, remaining, minute_remaining)

    def valid(self) -> bool:
        return self.origin <= time.monotonic() < self.deadline()


@contextmanager
def authorization_scope(
    callback: Callable[[], Authorization], *, state_path: Path | None = None
) -> Iterator[None]:
    token = _policy.set(callback)
    state_token = _state_path.set(state_path or _state_path.get())
    try:
        yield
    finally:
        _policy.reset(token)
        _state_path.reset(state_token)


@contextmanager
def account_lock(path: Path, account: str) -> Iterator[int]:
    import fcntl

    key = str(path.resolve()) + "\0" + account
    with _mutex_guard:
        mutex = _mutexes.setdefault(key, threading.RLock())
    with mutex:
        lock_path = path.with_name(
            path.name + "." + hashlib.sha256(key.encode()).hexdigest() + ".lock"
        )
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held = getattr(_held_locks, "keys", [])
            _held_locks.keys = held
            held.append(key)
            try:
                yield fd
            finally:
                held.remove(key)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def thread_holds_account_lock(path: Path | None = None) -> bool:
    """Detect nested identity writes before taking any identity/account lock."""
    if path is None:
        return bool(getattr(_held_locks, "keys", ()))
    prefix = str(path.resolve()) + "\0"
    return any(key.startswith(prefix) for key in getattr(_held_locks, "keys", ()))


def cooldown_bound() -> float:
    try:
        value = float(os.getenv("FETCHER_PROVIDER_MAX_RETRY_AFTER_SECONDS", "300"))
    except ValueError:
        raise QuotaBlockedError("provider cooldown configuration invalid") from None
    if not math.isfinite(value) or not 60 <= value <= 86400:
        raise QuotaBlockedError("provider cooldown configuration invalid")
    return value


def _windows(timestamp: float) -> tuple[str, str]:
    value = datetime.fromtimestamp(timestamp, timezone.utc)
    return value.date().isoformat(), value.strftime("%Y-%m-%dT%H:%M")


class Acquisition:
    def __init__(
        self,
        provider: str,
        *,
        credential: str | None = None,
        kind: str = "acquisition",
        state_path: Path | None = None,
        account: str | None = None,
        strict: bool = False,
    ) -> None:
        self.provider = provider
        self.kind = kind
        self.units = 0 if kind == "cleanup" else 1
        self.login_units = int(kind == "sdk_login")
        self.strict = strict
        self.offline = (
            not strict
            and state_path is None
            and _state_path.get() is None
            and not os.getenv("FETCHER_ACCOUNT_STATE_PATH")
            and not os.getenv("SOURCE_API_URL")
            and _policy.get() is None
        )
        import tempfile

        default_path = (
            str(Path(tempfile.mkdtemp(prefix="findb-offline-account-")) / "governor.sqlite3")
            if self.offline
            else "/var/lib/findb-account/governor.sqlite3"
        )
        self.state = FullMarketState(
            state_path
            or Path(os.getenv("FETCHER_ACCOUNT_STATE_PATH", str(_state_path.get() or default_path)))
        )
        self.account = account or account_identity(provider, self.state, credential=credential)
        self.value = limits(provider)
        self.interval = interval(self.value)
        if self.offline:
            self.interval = 0
            self.value["requests_per_minute"] = None
        self.bound = cooldown_bound()
        self.operation_id = os.urandom(16).hex()
        self.started_at: float | None = None
        self.possibly_started = False
        self.requests_before = 0
        self.requests_after = 0
        self.authorization: Authorization | None = None
        self.prepared_windows: tuple[str, str] | None = None
        self.lock_fd = -1
        self.quarantined = False
        self.entered = False
        self.outcome = "unknown"

    def __enter__(self) -> Acquisition:
        if self.entered:
            raise QuotaBlockedError("acquisition objects cannot be reused")
        self.entered = True
        self._lock = account_lock(self.state.path, self.account)
        self.lock_fd = self._lock.__enter__()
        try:
            self.recover()
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise
        return self

    def recover(self) -> None:
        now = time.time()
        with self.state.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM acquisition_intent WHERE account=? AND status='armed'",
                (self.account,),
            ).fetchall()
            for row in rows:
                if now < row["armed_at"]:
                    raise QuotaBlockedError("account clock moved backwards")
                db.execute(
                    "UPDATE acquisition_intent SET status='unknown',completed_at=? WHERE operation_id=? AND status='armed'",
                    (now, row["operation_id"]),
                )
                self._floor(db, now + max(row["cooldown_bound"], self.interval))

    def _floor(self, db: Any, until: float) -> None:
        db.execute(
            "INSERT INTO full_pacing VALUES(?,?) ON CONFLICT(account) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)",
            (self.account, until),
        )

    def prepare(self) -> None:
        while True:
            now = time.time()
            day, minute = _windows(now)
            wait = 0.0
            with self.state.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT next_at FROM full_pacing WHERE account=?", (self.account,)
                ).fetchone()
                prior = (
                    db.execute(
                        "SELECT MAX(blocked_until) FROM full_quota WHERE account=?", (self.account,)
                    ).fetchone()[0]
                    or 0.0
                )
                wait = (
                    0.0
                    if self.kind == "cleanup"
                    else max(0.0, max(row[0] if row else 0.0, prior) - now)
                )
                if not math.isfinite(wait):
                    raise QuotaBlockedError("account pacing is invalid")
                if not wait:
                    for window, request_limit, width in (
                        (day, self.value["requests_per_day"], 86400),
                        (minute, self.value["requests_per_minute"], 60),
                    ):
                        lower = now - now % width
                        liability = db.execute(
                            "SELECT COALESCE(SUM(request_units),0) FROM acquisition_intent WHERE account=? AND status='unknown' AND armed_at<? AND completed_at>=?",
                            (self.account, lower + width, lower),
                        ).fetchone()[0]
                        quota = db.execute(
                            "SELECT requests FROM full_quota WHERE account=? AND window=?",
                            (self.account, window),
                        ).fetchone()
                        used = quota[0] if quota else 0
                        if (
                            request_limit is not None
                            and self.units
                            and used + liability + self.units > request_limit
                        ):
                            raise QuotaBlockedError("durable account quota is exhausted")
                    if self.login_units:
                        self._check_login(db, now)
                    db.execute(
                        "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound,operation_kind,request_units,login_units) VALUES(?,?,?,?,'armed',?,?,?,?) ON CONFLICT(operation_id) DO UPDATE SET armed_at=excluded.armed_at WHERE status='armed'",
                        (
                            self.operation_id,
                            self.account,
                            self.provider,
                            now,
                            self.bound,
                            self.kind,
                            self.units,
                            self.login_units,
                        ),
                    )
                    self.prepared_windows = (day, minute)
                    day_row = db.execute(
                        "SELECT requests FROM full_quota WHERE account=? AND window=?",
                        (self.account, day),
                    ).fetchone()
                    self.requests_before = day_row[0] if day_row else 0
            if not wait:
                return
            self.authorization = None
            time.sleep(min(wait, 1.0))

    def _check_login(self, db: Any, now: float) -> None:
        future = db.execute(
            "SELECT COUNT(*) FROM acquisition_intent WHERE account=? AND login_units=1 AND (actual_start>? OR armed_at>? OR completed_at>?)",
            (self.account, now, now, now),
        ).fetchone()[0]
        if future:
            raise QuotaBlockedError("login clock moved backwards")
        used = db.execute(
            "SELECT COALESCE(SUM(login_units),0) FROM acquisition_intent WHERE account=? AND operation_id<>? AND ((actual_start>?) OR (status='unknown' AND actual_start IS NULL AND armed_at<=? AND completed_at>?))",
            (self.account, self.operation_id, now - 86400, now, now - 86400),
        ).fetchone()[0]
        if used + 1 > 1000:
            raise QuotaBlockedError("SDK login rolling 24h quota exhausted")
        latest = db.execute(
            "SELECT u.* FROM acquisition_usage u JOIN acquisition_intent i USING(operation_id) WHERE i.account=? AND u.connections IS NOT NULL ORDER BY observed_at DESC LIMIT 1",
            (self.account,),
        ).fetchone()
        if (
            latest
            and 0 <= now - latest["observed_at"] < SDK_SAMPLE_SECONDS
            and latest["connections"] >= 5
        ):
            raise QuotaBlockedError("SDK current connection limit reached")

    def authorize(self) -> Authorization:
        callback = _policy.get()
        if callback is not None:
            grant = callback()
        elif os.getenv("SOURCE_API_URL") and os.getenv("SOURCE_CLIENT_KEY"):
            from findb_fetcher.account_governor import consumer
            from findb_fetcher.config import FetcherConfig
            from findb_fetcher.full_market_client import FullMarketSourceClient

            if consumer() == "historical":
                raise QuotaBlockedError(
                    "historical acquisition requires current item authorization"
                )
            scheduler = {
                "twelve_data": "twelve_data_us_common_stocks_daily_v1",
                "finlab": "finlab_tw_equity_eod_v1",
                "shioaji": "shioaji_tw_pilot_v1",
                "taifex": "taifex_tw_futures_pilot_v1",
            }[self.provider]
            path = (
                "full-market/authorization?consumer=" + consumer()
                if consumer() in {"full_market", "maintenance"}
                else "scheduler-controls/" + scheduler + "/authorization"
            )
            with FullMarketSourceClient(FetcherConfig.from_env()) as source:
                grant = source.authorization("GET", path)
        elif (
            not self.strict
            and not os.getenv("FETCHER_ACCOUNT_STATE_PATH")
            and os.getenv("APP_ENVIRONMENT", "local") == "local"
        ):
            # Offline developer adapters have no installed shared account/control plane.
            now = time.time()
            grant = Authorization(time.monotonic(), now, now)
        else:
            raise QuotaBlockedError("current Source acquisition authorization unavailable")
        self.authorization = grant
        if grant.provider is not None and grant.provider != self.provider:
            raise QuotaBlockedError("Source acquisition provider scope mismatch")
        return grant

    def start(self) -> None:
        if self.started_at is not None:
            raise QuotaBlockedError("operation already started")
        if self.kind == "cleanup":
            self.prepare()
            self.started_at = time.time()
            self.requests_after = self.requests_before
            return
        for _ in range(3):
            self.prepare()
            grant = self.authorize()
            now = time.time()
            if grant.valid() and _windows(now) == self.prepared_windows:
                self.confirm_start(now)
                return
        raise QuotaBlockedError("authorization dispatch latency exceeds safe bound")

    def confirm_start(self, timestamp: float) -> None:
        if (
            self.authorization is None
            or not self.authorization.valid()
            or _windows(timestamp) != self.prepared_windows
            or not -0.250 <= timestamp - self.authorization.checked_at < REMOTE_AGE_SECONDS
            or (
                self.authorization.expires_at is not None
                and timestamp >= self.authorization.expires_at
            )
        ):
            raise QuotaBlockedError("acquisition authorization expired")
        self.started_at = timestamp
        self.requests_after = self.requests_before + self.units

    def rate_limited(self, retry_after: str | None = None) -> None:
        from email.utils import parsedate_to_datetime

        now = time.time()
        delay = 60.0
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(retry_after).timestamp() - now
                except (TypeError, ValueError, OverflowError):
                    delay = 60.0
        delay = min(self.bound, max(60.0, delay if math.isfinite(delay) else 60.0))
        with self.state.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._floor(db, now + delay)

    def usage(self, sample: dict[str, Any]) -> None:
        with self.state.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO acquisition_usage(operation_id,observed_at,bytes,limit_bytes,remaining_bytes,connections,sample_started_at) VALUES(?,?,?,?,?,?,?)",
                (
                    self.operation_id,
                    sample["observed_at"],
                    sample["bytes"],
                    sample["limit_bytes"],
                    sample["remaining_bytes"],
                    sample.get("connections"),
                    sample.get("sample_started_at"),
                ),
            )

    def finalize(self) -> None:
        completed = time.time()
        with self.state.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM acquisition_intent WHERE operation_id=?", (self.operation_id,)
            ).fetchone()
            if row is None or row[0] != "armed":
                return
            if self.started_at is None and self.possibly_started:
                self._floor(db, completed + self.bound)
                db.execute(
                    "UPDATE acquisition_intent SET status='unknown',completed_at=? WHERE operation_id=? AND status='armed'",
                    (completed, self.operation_id),
                )
                return
            if self.started_at is not None:
                if completed < self.started_at:
                    raise QuotaBlockedError("account clock moved backwards")
                for window in _windows(self.started_at) if self.units else ():
                    db.execute(
                        "INSERT INTO full_quota(account,window,requests,bytes) VALUES(?,?,?,0) ON CONFLICT(account,window) DO UPDATE SET requests=requests+excluded.requests",
                        (self.account, window, self.units),
                    )
                self._floor(db, completed + self.interval)
            db.execute(
                "UPDATE acquisition_intent SET status=?,actual_start=?,completed_at=?,outcome=? WHERE operation_id=? AND status='armed'",
                (
                    "finalized" if self.started_at is not None else "cancelled",
                    self.started_at,
                    completed,
                    self.outcome if self.started_at is not None else "cancelled",
                    self.operation_id,
                ),
            )

    def __exit__(self, *args: Any) -> None:
        if self.outcome == "unknown":
            self.outcome = "error" if args and args[0] is not None else "success"
        if self.quarantined:
            _quarantined.append(self._lock)
            return
        try:
            self.finalize()
        except BaseException:
            # Keep the descriptor locked until process termination. Releasing it
            # after failed durability would let a second consumer bypass debt.
            _quarantined.append(self._lock)
            raise
        self._lock.__exit__(*args)


_quarantined: list[Any] = []


@contextmanager
def http_acquisition(
    provider: str, client: Any, *, url: str, credential: str | None = None
) -> Iterator[tuple[Acquisition, dict[str, Any]]]:
    from findb_fetcher.http_boundary import extensions, prepare_transport

    prepare_transport(client, url)
    with Acquisition(provider, credential=credential) as operation:

        def headers(status: int, values: Any) -> None:
            if status == 429:
                operation.rate_limited(values.get("Retry-After"))

        yield operation, extensions(start=operation.start, headers=headers)
        if operation.started_at is None:
            raise QuotaBlockedError("HTTP transport did not expose acquisition boundary")
