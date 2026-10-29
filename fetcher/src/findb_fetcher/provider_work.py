"""Whole-work host exclusion and fenced Source ownership.

The shared inode is independent of provider account/acquisition locks. Never
LOCK_UN a descriptor shared with a child: closing our copy lets an orphan keep
the provider locked until its last inherited descriptor closes.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import inspect
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

_current: ContextVar[ProviderWorkSession | None] = ContextVar("provider_work", default=None)
_fd: ContextVar[int | None] = ContextVar("provider_work_fd", default=None)
_fd_identity: ContextVar[tuple[int, int] | None] = ContextVar(
    "provider_work_fd_identity", default=None
)
_quarantine: list[int] = []
P = ParamSpec("P")
T = TypeVar("T")


class ProviderWorkWaitingError(RuntimeError):
    """Ordinary FIFO/cooldown wait; never an acquisition failure."""


class ProviderWorkRecoveryBlockedError(BaseException):
    """A process boundary cannot be proven safe; ordinary retries must not catch it.

    Like cancellation, this must cross adapter error sanitizers and reach the
    whole-work context, which quarantines the FD and retains the DB owner.
    """


class ProviderWorkAcquisitionStopped(BaseException):
    """Cross SDK sanitizers without recording a provider failure on stop/cancel."""


def enabled() -> bool:
    """Compatibility assertion for callers; there is no legacy execution mode."""
    from findb_fetcher.config import require_provider_work_protocol

    require_provider_work_protocol()
    return True


def inherited_fds() -> tuple[int, ...]:
    current = _fd.get()
    return (current,) if current is not None else ()


def require_acquisition(provider: str, *, local_only: bool = False) -> bool:
    """Check the work fence; a local-only check never performs blocking RPC/DB I/O.

    False means the prior remote check is too old and needs a fresh bounded
    authorization attempt. Missing or changed ownership always raises.
    """
    enabled()
    owner = active()
    fd = _fd.get()
    identity = _fd_identity.get()
    if owner is None or owner.provider != provider or fd is None or identity is None:
        raise ProviderWorkRecoveryBlockedError(
            "provider acquisition requires whole-work owner and host descriptor"
        )
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != identity:
            raise ValueError
    except (OSError, ValueError):
        raise ProviderWorkRecoveryBlockedError("provider host descriptor differs") from None
    owner._check_local_fence()
    if local_only:
        from findb_fetcher.acquisition import REMOTE_AGE_SECONDS
        from findb_fetcher.acquisition import time as account_time

        return (
            owner._authorization_started is not None
            and owner._authorization_expires is not None
            and owner._authorization_checked is not None
            and owner._authorization_deadline is not None
            and owner._authorization_started
            <= account_time.monotonic()
            < owner._authorization_deadline
            and -0.250 <= account_time.time() - owner._authorization_checked < REMOTE_AGE_SECONDS
            and account_time.time() < owner._authorization_expires
            and owner._authorization_fd == (fd, *identity)
        )
    owner.authorize()
    owner._authorization_fd = (fd, *identity)
    return True


def acquisition_dispatch_deadline(provider: str, account_deadline: float) -> float:
    """Carry the tighter work bound through deferred SDK/IPC dispatch."""
    from findb_fetcher.acquisition import time as account_time

    if not require_acquisition(provider, local_only=True):
        return account_time.monotonic()
    owner = active()
    assert owner is not None and owner._authorization_deadline is not None
    return min(account_deadline, owner._authorization_deadline)


def active() -> ProviderWorkSession | None:
    return _current.get()


def require_owner(provider: str) -> ProviderWorkSession:
    """Require ownership even when resuming prepared bytes without new I/O."""
    enabled()
    owner = active()
    if owner is None or owner.provider != provider:
        raise ProviderWorkRecoveryBlockedError("provider execution requires a fenced work owner")
    owner._check_local_fence()
    return owner


def scoped_identity(seed: bytes) -> bytes:
    """Scope only newly constructed requests; prepared bytes are never rewritten."""
    owner = active()
    return seed + b"\0provider-work:" + owner.work["work_id"].encode() if owner else seed


def _open_host_lock(provider: str, *, root: Path | None = None) -> int:
    env = os.getenv("APP_ENVIRONMENT", "local")
    folder = (
        root
        or Path(
            os.getenv("FETCHER_ACCOUNT_STATE_PATH", "/var/lib/findb-account/governor.sqlite3")
        ).parent
    )
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"provider-work-{env}-{provider}.lock"
    return os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)


def initialize_host_lock(provider: str, *, root: Path | None = None) -> None:
    """Ensure the shared inode exists without acquiring or releasing ownership.

    A separate open file description can be closed while a sibling consumer
    owns the inode. Never truncate, replace, unlink or LOCK_UN the holder's file.
    """
    os.close(_open_host_lock(provider, root=root))


@contextmanager
def host_lock(provider: str, *, root: Path | None = None) -> Iterator[tuple[int, str]]:
    env = os.getenv("APP_ENVIRONMENT", "local")
    fd = _open_host_lock(provider, root=root)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProviderWorkWaitingError("provider host owner is active") from None
        os.set_inheritable(fd, True)
        stat = os.fstat(fd)
        identity = f"{env}:{stat.st_dev}:{stat.st_ino}"
        token = _fd.set(fd)
        identity_token = _fd_identity.set((stat.st_dev, stat.st_ino))
        try:
            yield fd, identity
        except ProviderWorkRecoveryBlockedError:
            _quarantine.append(fd)
            fd = -1
            raise
        finally:
            _fd.reset(token)
            _fd_identity.reset(identity_token)
    finally:
        if fd >= 0:
            os.close(fd)


class WorkJournal:
    """Stable-work frozen scope, prepared bytes and receipts survive queue retry."""

    def __init__(self, path: Path, stable_key: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.path, self.key = path, stable_key
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS work_manifest(work_key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS work_cooldown(work_key TEXT PRIMARY KEY, not_before REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS work_delivery(work_key TEXT NOT NULL, member_key TEXT NOT NULL,
                    body BLOB NOT NULL, sha256 TEXT NOT NULL, receipt TEXT, terminal TEXT,
                    PRIMARY KEY(work_key,member_key));
                CREATE TABLE IF NOT EXISTS work_job(work_key TEXT NOT NULL, job_key TEXT NOT NULL,
                    prepared TEXT NOT NULL, PRIMARY KEY(work_key,job_key));
                CREATE TABLE IF NOT EXISTS work_budget(work_key TEXT NOT NULL, operation_key TEXT NOT NULL, attempts INTEGER NOT NULL, PRIMARY KEY(work_key,operation_key));
                CREATE TABLE IF NOT EXISTS work_response(work_key TEXT NOT NULL, response_key TEXT NOT NULL,
                    body BLOB NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(work_key,response_key));
                CREATE TABLE IF NOT EXISTS work_response_observation(work_key TEXT NOT NULL, response_key TEXT NOT NULL,
                    observed_at REAL NOT NULL, origin TEXT NOT NULL, body BLOB NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS work_source_error(work_key TEXT NOT NULL, delivery_key TEXT NOT NULL,
                    observed_at REAL NOT NULL, status INTEGER NOT NULL, code TEXT, body_sha256 TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def freeze(self, manifest: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO work_manifest VALUES(?,?)", (self.key, encoded))
            row = db.execute(
                "SELECT body FROM work_manifest WHERE work_key=?", (self.key,)
            ).fetchone()
        return json.loads(row[0])

    def manifest(self) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT body FROM work_manifest WHERE work_key=?", (self.key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_prepared_request(self, job, request: dict, *, now: datetime) -> None:
        owner = active()
        if owner:
            owner.reserve(request, job_key=job.job_key)
        body = json.dumps(request, sort_keys=True, separators=(",", ":"))
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO work_job VALUES(?,?,?)", (self.key, job.job_key, body)
            )
            stored = db.execute(
                "SELECT prepared FROM work_job WHERE work_key=? AND job_key=?",
                (self.key, job.job_key),
            ).fetchone()
            if stored[0] != body:
                raise ProviderWorkRecoveryBlockedError("prepared job bytes changed")

    def prepared(self, job_key: str) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT prepared FROM work_job WHERE work_key=? AND job_key=?", (self.key, job_key)
            ).fetchone()
        if row:
            return json.loads(row[0])
        owner = active()
        if owner:
            bodies = owner.restore(job_key=job_key)
            if bodies:
                if len(bodies) != 1:
                    raise ProviderWorkRecoveryBlockedError("prepared job identity is ambiguous")
                request = json.loads(bodies[0])
                self.save_prepared_request(
                    type("Job", (), {"job_key": job_key})(), request, now=datetime.now(timezone.utc)
                )
                return request
        return None

    def source_error(self, delivery, status: int, code: str | None) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO work_source_error VALUES(?,?,?,?,?,?)",
                (
                    self.key,
                    delivery.idempotency_key,
                    time.time(),
                    status,
                    code,
                    hashlib.sha256(delivery.body).hexdigest(),
                ),
            )

    def latest_source_error(self) -> dict | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT delivery_key,status,code,body_sha256 FROM work_source_error WHERE work_key=? ORDER BY rowid DESC LIMIT 1",
                (self.key,),
            ).fetchone()
        return (
            dict(zip(("delivery_key", "http_status", "code", "body_sha256"), row, strict=True))
            if row
            else None
        )

    def prepare(self, member: str, body: bytes) -> None:
        digest = hashlib.sha256(body).hexdigest()
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO work_delivery(work_key,member_key,body,sha256) VALUES(?,?,?,?)",
                (self.key, member, body, digest),
            )
            row = db.execute(
                "SELECT sha256 FROM work_delivery WHERE work_key=? AND member_key=?",
                (self.key, member),
            ).fetchone()
            if row[0] != digest:
                raise ProviderWorkRecoveryBlockedError("prepared delivery bytes changed")

    def receipt(self, member: str, value: dict) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE work_delivery SET receipt=? WHERE work_key=? AND member_key=?",
                (json.dumps(value), self.key, member),
            )

    def response(self, key: str, body: bytes | None = None) -> bytes | None:
        with self.connection() as db:
            if body is not None:
                db.execute(
                    "INSERT OR IGNORE INTO work_response VALUES(?,?,?,?)",
                    (self.key, key, body, hashlib.sha256(body).hexdigest()),
                )
            row = db.execute(
                "SELECT body,sha256 FROM work_response WHERE work_key=? AND response_key=?",
                (self.key, key),
            ).fetchone()
            if row:
                raw = bytes(row[0])
                if hashlib.sha256(raw).hexdigest() != row[1]:
                    raise ProviderWorkRecoveryBlockedError(
                        "durable provider response checksum differs"
                    )
                return raw
        return None

    def observe_response(self, key: str, body: bytes, *, origin: str) -> None:
        """Keep rejected target observations without making them replay authority."""
        with self.connection() as db:
            db.execute(
                "INSERT INTO work_response_observation VALUES(?,?,?,?,?,?)",
                (self.key, key, time.time(), origin, body, hashlib.sha256(body).hexdigest()),
            )

    def has_response_observation(self, key: str, *, prefix: bool = False) -> bool:
        with self.connection() as db:
            row = db.execute(
                "SELECT 1 FROM work_response_observation "
                "WHERE work_key=? AND "
                + ("substr(response_key,1,?)=?" if prefix else "response_key=?")
                + " LIMIT 1",
                (self.key, len(key), key) if prefix else (self.key, key),
            ).fetchone()
        return row is not None

    def cooldown_until(self) -> float:
        with self.connection() as db:
            row = db.execute(
                "SELECT not_before FROM work_cooldown WHERE work_key=?", (self.key,)
            ).fetchone()
        return float(row[0]) if row else 0.0

    def observe_cooldown(self, timestamp: float) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO work_cooldown VALUES(?,?) ON CONFLICT(work_key) DO UPDATE SET not_before=MAX(not_before,excluded.not_before)",
                (self.key, timestamp),
            )

    def reserve_attempt(self, operation_key: str, maximum: int) -> int:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO work_budget VALUES(?,?,0)", (self.key, operation_key))
            count = db.execute(
                "SELECT attempts FROM work_budget WHERE work_key=? AND operation_key=?",
                (self.key, operation_key),
            ).fetchone()[0]
            if count >= maximum:
                raise ValueError("original provider operation budget exhausted")
            db.execute(
                "UPDATE work_budget SET attempts=attempts+1 WHERE work_key=? AND operation_key=?",
                (self.key, operation_key),
            )
            return count + 1

    def release_wait_attempt(self, operation_key: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE work_budget SET attempts=MAX(0,attempts-1) WHERE work_key=? AND operation_key=?",
                (self.key, operation_key),
            )


def durable_response(
    name: str,
    fields: tuple[str, ...],
    *,
    target_valid: Callable[[Any, dict[str, Any]], bool] | None = None,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Only target-valid native snapshots authorize replay; validation performs no I/O."""

    def decorate(method: Callable[P, T]) -> Callable[P, T]:
        signature = inspect.signature(method)

        def encode(value: T) -> bytes:
            kind = type(value).__name__
            if isinstance(value, bytes):
                data: Any = base64.b64encode(value).decode()
            elif hasattr(value, "raw_bytes"):
                data = {
                    "payload": value,
                    "raw_bytes": base64.b64encode(cast(Any, value).raw_bytes).decode(),
                }
            elif is_dataclass(value) and not isinstance(value, type):
                data = asdict(value)
            else:
                data = value

            def pack(item):
                if isinstance(item, bytes):
                    return {"__bytes__": base64.b64encode(item).decode()}
                if isinstance(item, dict):
                    return {key: pack(value) for key, value in item.items()}
                if isinstance(item, (list, tuple)):
                    return [pack(value) for value in item]
                return item

            encoded = json.dumps(
                {"type": kind, "data": pack(data)},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
            return encoded

        def decode(saved: bytes) -> T:
            def unpack(item):
                if isinstance(item, dict) and set(item) == {"__bytes__"}:
                    return base64.b64decode(item["__bytes__"], validate=True)
                if isinstance(item, dict):
                    return {key: unpack(value) for key, value in item.items()}
                if isinstance(item, list):
                    return [unpack(value) for value in item]
                return item

            snapshot = unpack(json.loads(saved))
            kind, data = snapshot["type"], snapshot["data"]
            if kind == "bytes":
                result: Any = base64.b64decode(data, validate=True)
            elif kind == "TwelveDataResponse":
                from findb_fetcher.providers.twelve_data import TwelveDataResponse

                result = TwelveDataResponse(
                    data["payload"], raw_bytes=base64.b64decode(data["raw_bytes"], validate=True)
                )
            elif kind == "FinLabDatasetTable":
                from findb_fetcher.providers.finlab import FinLabDatasetTable

                result = FinLabDatasetTable(
                    tuple(data["dates"]),
                    tuple(data["symbols"]),
                    tuple(tuple(row) for row in data["values"]),
                )
            elif kind == "ShioajiKbarsSnapshot":
                from findb_fetcher.providers.shioaji import ShioajiKbarsSnapshot

                result = ShioajiKbarsSnapshot(
                    **{
                        **data,
                        "kbars": {key: tuple(values) for key, values in data["kbars"].items()},
                    }
                )
            elif kind in {"dict", "list", "tuple"}:
                result = tuple(data) if kind == "tuple" else data
            else:
                raise ProviderWorkRecoveryBlockedError(
                    "durable provider response type is unsupported"
                )
            return cast(T, result)

        @wraps(method)
        def call(*args: P.args, **kwargs: P.kwargs) -> T:
            owner = active()
            if owner is None:
                return method(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            identity = json.dumps(
                {key: bound.arguments[key] for key in fields},
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            legacy_key = name + ":" + hashlib.sha256(identity.encode()).hexdigest()
            key = legacy_key + ":validated-target-v1" if target_valid else legacy_key
            saved = owner.journal.response(key)
            if saved is None and target_valid:
                # Pre-upgrade valid cache remains byte-for-byte authoritative.
                # Invalid legacy bytes remain in place as immutable evidence.
                saved = owner.journal.response(legacy_key)
            if saved is not None:
                result = decode(saved)
                if target_valid is None or target_valid(result, bound.arguments):
                    owner.journal.response(key, saved)
                    return result
                owner.journal.observe_response(legacy_key, saved, origin="legacy_cache")
            value = method(*args, **kwargs)
            encoded = encode(value)
            if target_valid is not None and not target_valid(value, bound.arguments):
                owner.journal.observe_response(legacy_key, encoded, origin="native")
            else:
                owner.journal.response(key, encoded)
            # The existing mapper classifies not-published vs permanent format;
            # neither its budget nor its public error contract changes here.
            return value

        return call

    return decorate


class ProviderWorkSession:
    def __init__(self, source, provider: str, work: dict[str, Any], journal: WorkJournal) -> None:
        self.source, self.provider, self.work, self.journal = source, provider, work, journal
        self._fence = {"generation": work["generation"], "token": work["token"]}
        self._work_identity = (provider, work["work_id"])
        self._authorization_started: float | None = None
        self._authorization_deadline: float | None = None
        self._authorization_checked: float | None = None
        self._authorization_expires: float | None = None
        self._authorization_fd: tuple[int, int, int] | None = None
        self.settled = False
        # Only current-reservation evidence; durable budgets/account debt remain
        # in their existing journals and survive a new session or process.
        self.dispatch_observations = 0
        self.failed = threading.Event()
        self._stop = threading.Event()
        self._heartbeat = threading.Thread(target=self._beat, daemon=True)
        self._heartbeat.start()

    @property
    def fence(self) -> dict[str, Any]:
        return dict(self._fence)

    def _check_local_fence(self) -> None:
        if (
            self.failed.is_set()
            or (self.provider, self.work["work_id"]) != self._work_identity
            or any(self.work[key] != value for key, value in self._fence.items())
        ):
            raise ProviderWorkRecoveryBlockedError(
                "provider work heartbeat or generation is unavailable"
            )

    def request(self, action: str, **body) -> dict:
        return self.source.request(
            "POST", f"provider-work/{self.work['work_id']}/{action}", {**self.fence, **body}
        )

    def publish_cooldown(self) -> None:
        self._check_local_fence()
        from findb_fetcher.acquisition import time as account_time

        remaining = self.journal.cooldown_until() - account_time.time()
        if remaining > 0:
            try:
                self.request(
                    "heartbeat",
                    acquisition_not_before=(
                        datetime.now(timezone.utc) + timedelta(seconds=remaining)
                    ).isoformat(),
                )
            except Exception as exc:
                raise ProviderWorkRecoveryBlockedError(
                    "account cooldown acknowledgement is unavailable"
                ) from exc

    def _beat(self) -> None:
        while not self._stop.wait(20):
            try:
                self.request("heartbeat")
            except Exception:
                self.failed.set()
                return

    def authorize(self) -> None:
        self._check_local_fence()
        from findb_fetcher.acquisition import REMOTE_AGE_SECONDS
        from findb_fetcher.acquisition import time as account_time
        from findb_fetcher.full_market_client import FullMarketProtocolError

        self._authorization_started = None
        started, started_utc = account_time.monotonic(), account_time.time()
        try:
            response = self.request("authorize")
        except FullMarketProtocolError as exc:
            if exc.code == "PROVIDER_WORK_SUSPENDED":
                raise ProviderWorkAcquisitionStopped("provider acquisition is stopped") from None
            raise ProviderWorkRecoveryBlockedError(
                "provider acquisition fence is unavailable"
            ) from exc
        try:
            if not isinstance(response, dict) or type(response.get("generation")) is not int:
                raise ValueError
            if any(
                response.get(key) != value
                for key, value in {
                    "work_id": self._work_identity[1],
                    "provider": self.provider,
                    **self.fence,
                }.items()
            ):
                raise ValueError
            checked = datetime.fromisoformat(response["checked_at"])
            expires = datetime.fromisoformat(response["expires_at"])
            if checked.tzinfo is None or expires.tzinfo is None or expires <= checked:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProviderWorkRecoveryBlockedError(
                "provider work authorization evidence is invalid"
            ) from None
        # The Source RPC itself can block while heartbeat/recovery changes.
        self._check_local_fence()
        from findb_fetcher.full_market_state import QuotaBlockedError

        if self.journal.cooldown_until() > account_time.time():
            raise QuotaBlockedError("original provider work cooldown is pending", quota_wait=True)
        # Anchor before the RPC: a delayed response cannot refresh an old check.
        self._authorization_started = started
        self._authorization_checked = checked.timestamp()
        self._authorization_expires = expires.timestamp()
        self._authorization_deadline = started + min(
            REMOTE_AGE_SECONDS,
            expires.timestamp() - started_utc,
            (expires - checked).total_seconds(),
            REMOTE_AGE_SECONDS - max(0.0, started_utc - checked.timestamp()),
        )

    def headers(self, prepared) -> dict[str, str]:
        if self.failed.is_set():
            raise ProviderWorkRecoveryBlockedError("provider work heartbeat is unavailable")
        self.journal.prepare(prepared.idempotency_key, prepared.body)
        self._preparation_request("prepare", body=prepared.body.decode(), stage="confirm")
        return {
            "X-FinDB-Work-Id": self.work["work_id"],
            "X-FinDB-Work-Generation": str(self.work["generation"]),
            "X-FinDB-Work-Token": self.work["token"],
            "X-FinDB-Work-Member": prepared.idempotency_key,
        }

    def freeze(self, manifest: dict[str, Any]) -> dict[str, Any]:
        frozen = self.journal.freeze(self.work.get("manifest") or manifest)
        self.request("manifest", manifest=frozen)
        self.work["manifest"] = frozen
        return frozen

    def reserve(self, request: dict, *, job_key: str | None = None) -> None:
        body = json.dumps(
            request, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
        self._preparation_request("prepare", body=body, stage="intent", job_key=job_key)

    def drain_prepared(self, *, pending_only: bool = False, skip: set[str] | None = None) -> list:
        """Replay only server-retained original bytes; never reconstruct or fetch."""
        from findb_fetcher.client import PreparedDelivery, SourceAPIClient
        from findb_fetcher.contracts import ContractRegistry

        receipts = []
        with SourceAPIClient(
            self.source.config,
            ContractRegistry(self.source.config.contracts_dir),
            client=self.source.client,
        ) as delivery:
            for key in self.prepared_keys(pending_only=pending_only):
                if key in (skip or set()):
                    continue
                bodies = self._preparation_request("prepared", delivery_key=key)["bodies"]
                if len(bodies) != 1:
                    raise ProviderWorkRecoveryBlockedError(
                        "original prepared identity is ambiguous"
                    )
                body = bodies[0]
                request = json.loads(body)
                prepared = PreparedDelivery(
                    body.encode(),
                    request["idempotency_key"],
                    request["dataset_key"],
                    request["schema_id"],
                    request["schema_version"],
                )
                receipts.append(delivery.deliver(prepared))
        return receipts

    def prepared_keys(self, *, pending_only: bool = False) -> list[str]:
        options = {"pending_only": True} if pending_only else {}
        return self._preparation_request("prepared", keys_only=True, **options)["member_keys"]

    def restore(self, *, job_key: str | None = None, member_key: str | None = None) -> list[str]:
        return self._preparation_request("prepared", job_key=job_key, member_key=member_key)[
            "bodies"
        ]

    def _preparation_request(self, action: str, **body) -> dict:
        from findb_fetcher.full_market_client import FullMarketProtocolError

        try:
            return self.request(action, **body)
        except FullMarketProtocolError as exc:
            if exc.status_code is None or exc.status_code >= 500:
                raise ProviderWorkRecoveryBlockedError(
                    "durable preparation acknowledgement is unavailable"
                ) from exc
            raise

    def settle(
        self,
        outcome: str,
        *,
        proof: dict | None = None,
        delay: int = 60,
        failure_code: str | None = None,
    ) -> None:
        self.publish_cooldown()
        self.request("close")
        body: dict[str, Any] = {
            "outcome": outcome,
            "proof": proof or {},
            "failure_code": failure_code,
        }
        if outcome == "retry":
            import math

            from findb_fetcher.acquisition import time as account_time

            if not body["proof"].get("pending_source_errors"):
                delay = max(delay, math.ceil(self.journal.cooldown_until() - account_time.time()))
            body["retry_at"] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        from findb_fetcher.full_market_client import FullMarketProtocolError

        while True:
            if self.failed.is_set():
                raise ProviderWorkRecoveryBlockedError("provider work heartbeat is unavailable")
            if outcome == "retry":
                body["retry_at"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat()
            try:
                self.request("settle", **body)
                self.settled = True
                return
            except FullMarketProtocolError as exc:
                if exc.code == "PROVIDER_WORK_PREPARED_DRAINING":
                    self.drain_prepared()
                    continue
                if exc.code != "PROVIDER_WORK_DRAINING":
                    raise ProviderWorkRecoveryBlockedError(
                        "provider settlement requires recovery"
                    ) from exc
                # Retain the host descriptor and DB owner through every accepted
                # normalization, including historical retries and raw reruns.
                time.sleep(2)

    def stop(self) -> None:
        self._stop.set()
        self._heartbeat.join(timeout=30)
        if self._heartbeat.is_alive():
            raise ProviderWorkRecoveryBlockedError("heartbeat thread has not stopped")


@contextmanager
def claimed_work(
    source,
    provider: str,
    *,
    work_id: str | None = None,
    accepted_kinds: list[str] | None = None,
    execution_profile: str | None = None,
    root: Path | None = None,
) -> Iterator[ProviderWorkSession | None]:
    enabled()
    with host_lock(provider, root=root) as (_, inode):
        claimed = source.request(
            "POST",
            "provider-work/claim",
            {
                "protocol": "provider-work-v1",
                "host_lock_identity": inode,
                "recover": True,
                "requested_work_id": work_id,
                "accepted_kinds": accepted_kinds,
                "execution_profile": execution_profile,
            },
        )
        work = claimed.get("work")
        if work is None:
            yield None
            return
        journal = WorkJournal(
            (
                root
                or Path(
                    os.getenv(
                        "FETCHER_ACCOUNT_STATE_PATH", "/var/lib/findb-account/governor.sqlite3"
                    )
                ).parent
            )
            / "provider-work.sqlite3",
            work["work_id"],
        )
        session = ProviderWorkSession(source, provider, work, journal)
        token = _current.set(session)
        try:
            session.publish_cooldown()
            yield session
        finally:
            _current.reset(token)
            session.stop()
            if not session.settled:
                raise ProviderWorkRecoveryBlockedError(
                    "provider work exited before durable settlement"
                )
