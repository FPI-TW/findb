"""Bounded blocking SDK execution; no timeout is treated as process death."""

from __future__ import annotations

import multiprocessing
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

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


def _wait(process: subprocess.Popen[bytes], timeout: float) -> None:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def supervised_call(
    operation: str,
    payload: dict[str, Any],
    *,
    timeout: float = 1800,
    checkpoint: Callable[[], float] | None = None,
) -> Any:
    """Run one allowlisted ``provider_worker`` operation in a fresh exec'd process.

    Credentials travel only through the pipe. The worker sends READY before
    each native I/O boundary; the parent answers with an absolute monotonic
    deadline, which the worker checks immediately before dispatch.
    """
    parent, child = multiprocessing.Pipe()
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "findb_fetcher.provider_worker", str(child.fileno())],
            pass_fds=(child.fileno(), *inherited_fds()),
            env=provider_environment(),
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
    value = None
    succeeded = False
    failure: str | None = None
    natural_exit: int | None = None
    try:
        expires = time.monotonic() + timeout
        parent.send((operation, payload, checkpoint is not None))
        while parent.poll(max(0, expires - time.monotonic())):
            try:
                event, value = parent.recv()
            except EOFError:
                break
            if event == "ready" and checkpoint is not None:
                parent.send(checkpoint())
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
    finally:
        if not succeeded and failure is None:
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
        _wait(process, 2)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        _wait(process, 2)
        parent.close()
        if process.poll() is None:
            raise ProviderWorkRecoveryBlockedError("blocking provider child has not been reaped")
        deadline = time.monotonic() + 2
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
    if not succeeded:
        if failure is None:
            failure = "killed" if natural_exit is not None and natural_exit < 0 else "crashed"
        raise ProviderProcessError("isolated provider SDK call failed", failure)
    return value
