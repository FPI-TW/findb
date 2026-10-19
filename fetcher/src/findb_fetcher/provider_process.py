"""Bounded blocking SDK execution; no timeout is treated as process death."""

from __future__ import annotations

import multiprocessing
import os
import signal
import time
from collections.abc import Callable
from typing import TypeVar, cast

from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

T = TypeVar("T")


class ProviderProcessError(RuntimeError):
    pass


def supervised_call(
    callback: Callable[[], T],
    *,
    timeout: float = 1800,
    initialize: Callable[[], None] | None = None,
    checkpoint: Callable[[], float] | None = None,
) -> T:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()

    def authorize() -> bool:
        if checkpoint is None:
            return True
        child.send(("ready", None))
        deadline = child.recv()
        # No child-side retry: expired IPC grants cannot multiply the parent's
        # bounded authorization attempts or permit delayed native dispatch.
        if time.monotonic() >= deadline:
            child.send(("expired", None))
            return False
        return True

    def run() -> None:
        parent.close()
        os.setsid()
        try:
            if initialize is not None:
                if not authorize():
                    return
                initialize()
            if authorize():
                child.send(("result", callback()))
        except BaseException:
            child.send(("error", None))
        finally:
            child.close()

    process = context.Process(target=run, daemon=False)
    process.start()
    child.close()
    assert process.pid is not None
    value = None
    succeeded = False
    try:
        expires = time.monotonic() + timeout
        while parent.poll(max(0, expires - time.monotonic())):
            try:
                event, value = parent.recv()
            except EOFError:
                break
            if event == "ready" and checkpoint is not None:
                parent.send(checkpoint())
                continue
            succeeded = event == "result"
            break
    finally:
        # Terminate the entire SDK process group, including descendants that
        # might otherwise outlive a successful return. The inherited whole-work
        # descriptor remains held by any child until its actual death.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            # A group can disappear while its leader exits (including EPERM
            # on macOS). A signal error is not death evidence: still join,
            # escalate, then prove both leader and group have disappeared.
            pass
        process.join(2)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.join(2)
        parent.close()
        if process.is_alive() or process.exitcode is None:
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
        process.close()
    if not succeeded:
        raise ProviderProcessError("isolated provider SDK call failed")
    return cast(T, value)
