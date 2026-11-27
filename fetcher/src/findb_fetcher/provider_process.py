"""Bounded blocking SDK execution; no timeout is treated as process death."""

from __future__ import annotations

import atexit
import multiprocessing
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any

from findb_fetcher import rpc_timing
from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError, inherited_fds

_FAILURE_CODE = re.compile(r"[a-z_]{1,24}")


class ProviderProcessError(RuntimeError):
    """``code`` is a bounded failure class; the message never carries provider text.

    The worker reports its own class (see ``provider_worker.failure_code``). The
    parent adds the ones only it can see: ``process_timeout`` (no answer before
    the deadline), ``dispatch_expired`` (the grant expired before native I/O),
    ``killed`` (the worker died from a signal, as after an OOM kill) and
    ``crashed`` (it exited without answering).
    """

    def __init__(self, message: str, code: str = "sdk_error") -> None:
        super().__init__(message)
        self.code = code


def provider_environment() -> dict[str, str]:
    # Explicit launch environment: no inherited Source/cloud/other-provider secrets.
    names = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
    values = {name: os.environ[name] for name in names if name in os.environ}
    from findb_fetcher.config import require_provider_work_protocol

    require_provider_work_protocol()
    values["PROVIDER_WORK_REQUIRED"] = "true"
    return values


def _ms(since: float) -> float:
    return (time.monotonic() - since) * 1000


def _record_phase(operation: str, value: Any) -> None:
    """Add one worker step (``provider_worker.phase``) to the delivery timing line.

    Diagnostics only: a malformed report is dropped rather than trusted, so the
    line keeps fixed names and the call's outcome never depends on it.
    """
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and _FAILURE_CODE.fullmatch(value[0])
        and isinstance(value[1], float)
        and 0 <= value[1] < 86_400_000
    ):
        rpc_timing.record(f"worker {operation} {value[0]}", value[1])


# How long each termination step waits for the worker and its group to die.
_REAP_GRACE_SECONDS = 2.0


def _wait(process: subprocess.Popen[bytes], timeout: float) -> None:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


# A reused worker serves at most this many calls, then a fresh one starts: the
# SDK's memory is returned and a long round never runs in one aged interpreter.
REUSE_CALL_LIMIT = 200
# Idle reused workers kept per consumer; FinLab intraday dispatches two at once.
REUSE_POOL_SIZE = 2


@dataclass
class _Worker:
    operation: str
    process: subprocess.Popen[bytes]
    conn: Connection
    # (fd, st_dev, st_ino) of the whole-work descriptor the worker inherited.
    descriptors: tuple[tuple[int, int, int], ...]
    environment: dict[str, str]
    spawned: float
    calls: int = 0


_pool_lock = threading.Lock()
_idle: list[_Worker] = []


def _descriptor_identity() -> tuple[tuple[int, int, int], ...]:
    identity = []
    for fd in inherited_fds():
        try:
            status = os.fstat(fd)
            identity.append((fd, status.st_dev, status.st_ino))
        except OSError:
            identity.append((fd, -1, -1))
    return tuple(identity)


def _spawn(operation: str) -> _Worker:
    parent, child = multiprocessing.Pipe()
    spawned = time.monotonic()
    descriptors = _descriptor_identity()
    environment = provider_environment()
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "findb_fetcher.provider_worker", str(child.fileno())],
            pass_fds=(child.fileno(), *inherited_fds()),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except BaseException:
        parent.close()
        raise
    finally:
        child.close()
    return _Worker(operation, process, parent, descriptors, environment, spawned)


def _borrow(operation: str) -> _Worker | None:
    """An idle worker of this operation started like a fresh one would be now."""
    identity = _descriptor_identity()
    environment = provider_environment()
    dead: list[_Worker] = []
    found = None
    with _pool_lock:
        for worker in list(_idle):
            if (
                worker.operation == operation
                and worker.descriptors == identity
                and worker.environment == environment
            ):
                _idle.remove(worker)
                if worker.process.poll() is None:
                    found = worker
                    break
                dead.append(worker)
    for worker in dead:
        _terminate(worker, settle=False)
    return found


def _give_back(worker: _Worker) -> None:
    with _pool_lock:
        if worker.calls < REUSE_CALL_LIMIT and len(_idle) < REUSE_POOL_SIZE:
            _idle.append(worker)
            return
    _retire(worker)


def _retire(worker: _Worker) -> None:
    """End an idle worker: ask it to exit, then reap it and its group as after any call."""
    try:
        worker.conn.send(None)
    except OSError:
        pass
    _terminate(worker, settle=True)


def close_reusable() -> None:
    """End every idle reused worker; called before the whole-work descriptor closes.

    A worker keeps the inherited host lock descriptor open while it waits for the
    next call, so it must be gone before the lane's lock is released: otherwise
    the next claim of the same lane would find the host owner still active.
    """
    with _pool_lock:
        workers = list(_idle)
        _idle.clear()
    for worker in workers:
        _retire(worker)


atexit.register(close_reusable)


def _terminate(worker: _Worker, *, settle: bool) -> int | None:
    """Reap the worker and its whole process group; return its natural exit status."""
    process = worker.process
    if settle:
        # The pipe closed without an answer: let the worker's own exit status
        # arrive before the group is signalled, so a kill is told from a crash.
        _wait(process, 0.5)
    natural_exit = process.poll()
    # Terminate the entire SDK process group, including descendants that
    # might otherwise outlive a successful return. The inherited whole-work
    # descriptor remains held by any child until its actual death.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        # A group can disappear while its leader exits (including EPERM
        # on macOS). A signal error is not death evidence: still wait,
        # escalate, then prove both leader and group have disappeared.
        pass
    _wait(process, _REAP_GRACE_SECONDS)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    _wait(process, _REAP_GRACE_SECONDS)
    worker.conn.close()
    if process.poll() is None:
        raise ProviderWorkRecoveryBlockedError("blocking provider child has not been reaped")
    deadline = time.monotonic() + _REAP_GRACE_SECONDS
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        except OSError as exc:
            raise ProviderWorkRecoveryBlockedError(
                "provider process group termination cannot be verified"
            ) from exc
        if time.monotonic() >= deadline:
            raise ProviderWorkRecoveryBlockedError("provider descendant process group remains")
        time.sleep(0.02)
    return natural_exit


def supervised_call(
    operation: str,
    payload: dict[str, Any],
    *,
    timeout: float = 1800,
    checkpoint: Callable[[], float] | None = None,
    reuse: bool = False,
) -> Any:
    """Run one allowlisted ``provider_worker`` operation in an exec'd process.

    Credentials travel only through the pipe. The worker sends READY before
    each native I/O boundary; the parent answers with an absolute monotonic
    deadline, which the worker checks immediately before dispatch.

    By default every call gets a fresh process, reaped with its group afterwards.
    With ``reuse`` a worker that answered successfully waits for the next call of
    the same operation under the same whole-work descriptor (see
    ``close_reusable``); any other outcome reaps it exactly as before (#753).
    """
    worker = _borrow(operation) if reuse else None
    fresh = worker is None
    if worker is None:
        worker = _spawn(operation)
    started = worker.spawned if fresh else time.monotonic()
    if not fresh:
        rpc_timing.record(f"worker {operation} reused", 0.0)
    value = None
    received = False
    succeeded = False
    failure: str | None = None
    try:
        expires = time.monotonic() + timeout
        worker.conn.send((operation, payload, checkpoint is not None, reuse))
        while worker.conn.poll(max(0, expires - time.monotonic())):
            try:
                event, value = worker.conn.recv()
            except EOFError:
                break
            if not received:
                received = True
                if fresh:
                    rpc_timing.record(f"worker {operation} first_message", _ms(started))
            if event == "ready" and checkpoint is not None:
                worker.conn.send(checkpoint())
                continue
            if event == "phase":
                _record_phase(operation, value)
                continue
            succeeded = event == "result"
            if event == "expired":
                failure = "dispatch_expired"
            elif event == "error":
                failure = (
                    value if isinstance(value, str) and _FAILURE_CODE.fullmatch(value) else None
                ) or "sdk_error"
            break
        else:
            failure = "process_timeout"
    except (BrokenPipeError, ConnectionResetError):
        # The worker exited before reading its request; reap it below.
        pass
    except BaseException:
        _terminate(worker, settle=True)
        raise
    answered = time.monotonic()
    rpc_timing.record(f"worker {operation} run", (answered - started) * 1000)
    if succeeded and reuse:
        worker.calls += 1
        _give_back(worker)
        return value
    natural_exit = _terminate(worker, settle=not succeeded and failure is None)
    rpc_timing.record(f"worker {operation} teardown", _ms(answered))
    if not succeeded:
        if failure is None:
            failure = "killed" if natural_exit is not None and natural_exit < 0 else "crashed"
        raise ProviderProcessError("isolated provider SDK call failed", failure)
    return value
