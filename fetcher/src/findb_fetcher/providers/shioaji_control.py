"""Key-free SDK operation handshake, actual starts and protected cleanup."""

from __future__ import annotations

import json
import os
import time
from multiprocessing.reduction import recv_handle, send_handle
from pathlib import Path
from typing import Any

from findb_fetcher.acquisition import SDK_SAMPLE_SECONDS, Acquisition, _windows
from findb_fetcher.full_market_state import QuotaBlockedError
from findb_fetcher.platform_limits import SHIOAJI_DAILY_BYTES


def usage_sample(api: Any) -> dict[str, Any]:
    began = time.time()
    usage = api.usage()
    values: dict[str, Any] = {
        name: usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        for name in ("bytes", "limit_bytes", "remaining_bytes", "connections")
    }
    if any(
        type(values[name]) is not int or values[name] < 0
        for name in ("bytes", "limit_bytes", "remaining_bytes")
    ):
        raise QuotaBlockedError("SDK current account usage unavailable")
    if values["bytes"] + values["remaining_bytes"] != values["limit_bytes"]:
        raise QuotaBlockedError("SDK account usage inconsistent")
    if values["connections"] is not None and (
        type(values["connections"]) is not int or values["connections"] < 0
    ):
        raise QuotaBlockedError("SDK connection observation invalid")
    observed = time.time()
    if not 0 <= observed - began < SDK_SAMPLE_SECONDS:
        raise QuotaBlockedError("SDK account usage sample stale")
    values.update({"observed_at": observed, "sample_started_at": began})
    return values


def child_operation(metadata: dict[str, Any], *, kind: str | None = None) -> Acquisition:
    operation = Acquisition(
        "shioaji",
        kind=kind or metadata.get("kind", "kbars"),
        state_path=Path(metadata["path"]),
        account=metadata["account"],
        strict=True,
    )
    operation.interval = metadata["interval"]
    return operation


class ChildBoundary:
    def __init__(self, conn: Any, metadata: dict[str, Any]) -> None:
        self.conn, self.metadata = conn, metadata
        self.fd = recv_handle(conn)
        if os.getenv("PROVIDER_WORK_REQUIRED") == "true" or metadata.get("work_required"):
            work_fd = metadata.get("work_fd")
            identity = metadata.get("work_identity")
            try:
                if type(work_fd) is not int or not isinstance(identity, list) or len(identity) != 2:
                    raise ValueError
                info = os.fstat(work_fd)
                if (info.st_dev, info.st_ino) != tuple(identity):
                    raise ValueError
            except (TypeError, OSError, ValueError):
                os.close(self.fd)
                raise QuotaBlockedError("whole-work descriptor is unavailable") from None
        self.started_at: float | None = None
        self.before: dict[str, Any] | None = None
        self.after: dict[str, Any] | None = None

    def start(self, api: Any = None) -> None:
        kind = self.metadata.get("kind", "kbars")
        if kind == "cleanup":
            self.started_at = time.time()
            return
        for _ in range(3):
            sample = usage_sample(api) if api is not None else None
            if sample is not None:
                if int(sample["sample_started_at"] // 86400) != int(sample["observed_at"] // 86400):
                    continue
                if sample["bytes"] >= SHIOAJI_DAILY_BYTES:
                    raise QuotaBlockedError(
                        "SDK daily account byte quota exhausted", quota_wait=True
                    )
                if type(sample["connections"]) is not int or not 1 <= sample["connections"] <= 5:
                    raise QuotaBlockedError("SDK authenticated connections unavailable or invalid")
            self.conn.send_bytes(
                json.dumps(
                    {
                        "stage": "ready",
                        "usage": sample,
                        "usage_state": "observed" if sample else "not_authenticated",
                    }
                ).encode()
            )
            go = json.loads(self.conn.recv_bytes(4096))
            if go.get("stage") == "cancel":
                raise QuotaBlockedError("acquisition authorization denied")
            if go.get("stage") != "go" or go.get("operation_id") != self.metadata["operation_id"]:
                raise QuotaBlockedError("invalid acquisition handshake")
            now, mono = time.time(), time.monotonic()
            if (
                not go["origin"] <= mono < go["deadline"]
                or list(_windows(now)) != go["windows"]
                or not -0.250 <= now - go["checked_at"] < SDK_SAMPLE_SECONDS
                or (go["lease_expires_at"] is not None and now >= go["lease_expires_at"])
                or (
                    sample is not None
                    and (
                        not 0 <= now - sample["observed_at"] < SDK_SAMPLE_SECONDS
                        or int(now // 86400) != int(sample["observed_at"] // 86400)
                    )
                )
            ):
                self.conn.send_bytes(b'{"stage":"retry"}')
                continue
            self.before, self.started_at = sample, now
            return
        raise QuotaBlockedError("authorization dispatch latency exceeds safe bound")

    def invoke(self, api: Any, call: Any, contract: Any, day: str) -> Any:
        self.start(api)
        return call(contract, start=day, end=day)

    def finish(self, api: Any = None, *, outcome: str = "success") -> None:
        if self.started_at is None:
            return
        operation = child_operation(self.metadata)
        operation.operation_id = self.metadata["operation_id"]
        operation.started_at = self.started_at
        operation.outcome = outcome
        try:
            self.after = usage_sample(api) if api is not None else None
        except Exception:
            self.after = None
        for sample in (self.before, self.after):
            if sample is not None:
                operation.usage(sample)
        operation.finalize()

    def close(self) -> None:
        os.close(self.fd)


def cleanup_session(
    session: Any, metadata: dict[str, Any], *, inherited_fd: int | None = None
) -> None:
    if not session.authentication_attempted:
        session.close()
        return
    operation = child_operation(metadata, kind="cleanup")
    if inherited_fd is not None:
        # Already-owning open file description: never flock a second conflicting FD.
        operation.start()
        try:
            session.close()
            operation.outcome = "error" if session.cleanup_failed else "success"
        finally:
            operation.finalize()
    else:
        with operation:
            operation.start()
            session.close()
            operation.outcome = "error" if session.cleanup_failed else "success"


def parent_fetch(
    conn: Any,
    process: Any,
    command: dict[str, Any],
    operation: Acquisition,
    *,
    timeout: float,
    max_bytes: int,
) -> dict[str, Any]:
    from findb_fetcher.provider_work import enabled, inherited_fds, require_acquisition

    require_acquisition("shioaji")
    descriptors = inherited_fds()
    work_fd = descriptors[0] if descriptors else None
    work_stat = os.fstat(work_fd) if work_fd is not None else None
    operation.prepare()  # Durable intent before child preparation/FD/READY.
    require_acquisition("shioaji")  # Pacing may have blocked before even child usage sampling.
    command = {
        **command,
        "governor": {
            "path": str(operation.state.path),
            "account": operation.account,
            "operation_id": operation.operation_id,
            "interval": operation.interval,
            "kind": operation.kind,
            "work_required": enabled(),
            "work_fd": work_fd,
            "work_identity": [work_stat.st_dev, work_stat.st_ino] if work_stat else None,
        },
    }
    conn.send_bytes(json.dumps(command).encode())
    send_handle(conn, operation.lock_fd, process.pid)
    attempts = 0
    try:
        while True:
            if not conn.poll(timeout):
                raise TimeoutError("isolated provider handshake timeout")
            message = json.loads(conn.recv_bytes(max_bytes))
            if message.get("stage") == "retry":
                operation.possibly_started = False
                continue
            if message.get("stage") == "ready":
                attempts += 1
                if attempts > 3:
                    raise QuotaBlockedError("authorization dispatch latency exceeds safe bound")
                sample = message["usage"]
                if sample is not None:
                    operation.usage(sample)
                with operation.state.connection() as db:
                    db.execute(
                        "UPDATE acquisition_intent SET usage_before_state=? WHERE operation_id=?",
                        (message.get("usage_state", "observed"), operation.operation_id),
                    )
                require_acquisition("shioaji")
                operation.prepare()
                grant = operation.authorize()
                require_acquisition("shioaji")
                fresh = grant.valid() and (
                    sample is None or 0 <= time.time() - sample["observed_at"] < SDK_SAMPLE_SECONDS
                )
                conn.send_bytes(
                    json.dumps(
                        {
                            "stage": "go",
                            "operation_id": operation.operation_id,
                            "origin": grant.origin,
                            "deadline": grant.deadline() if fresh else grant.origin,
                            "windows": list(operation.prepared_windows or ()),
                            "checked_at": grant.checked_at,
                            "lease_expires_at": grant.expires_at,
                        }
                    ).encode()
                )
                operation.possibly_started = fresh
                continue
            if message.get("stage") != "result":
                raise QuotaBlockedError("isolated provider acquisition failed")
            try:
                conn.send_bytes(b'{"stage":"ack"}')
            except (OSError, EOFError):
                pass  # Durable finalization already precedes result delivery.
            operation.started_at = message.get("actual_start")
            operation.requests_after = operation.requests_before + (
                operation.units if operation.started_at is not None else 0
            )
            if message.get("error"):
                waiting = message.get("quota_wait") is True and operation.started_at is None
                if waiting:
                    operation.possibly_started = False
                raise QuotaBlockedError("isolated provider acquisition failed", quota_wait=waiting)
            return message["payload"]
    except BaseException:
        try:
            conn.send_bytes(b'{"stage":"cancel"}')
        except (OSError, EOFError):
            pass
        raise
