"""Exec'd worker for one allowlisted blocking provider operation.

``provider_process.supervised_call`` starts this module with a private pipe and
the inherited whole-work descriptor. The operation name selects a fixed
module-level function; no code, import path or callable crosses the pipe.
"""

from __future__ import annotations

import importlib
import re
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection
from typing import Any

OPERATIONS = {
    "finlab.get": ("findb_fetcher.providers.finlab", "native_get"),
    "finlab.usage": ("findb_fetcher.providers.finlab", "native_usage"),
    "finlab.security_table": ("findb_fetcher.providers.finlab", "native_security_table"),
    "finlab.intraday": ("findb_fetcher.providers.finlab_intraday", "native_intraday"),
    "factset.query": ("findb_fetcher.providers.factset", "native_query"),
}


_FAILURE_CODE = re.compile(r"[a-z_]{1,24}")
_TIMEOUT_NAMES = frozenset({"Timeout", "ReadTimeout", "ConnectTimeout", "ReadTimeoutError"})
_NETWORK_NAMES = frozenset(
    {
        "ConnectionError",
        "SSLError",
        "ProxyError",
        "ChunkedEncodingError",
        "MaxRetryError",
        "NewConnectionError",
        "ProtocolError",
        "gaierror",
    }
)


_PHASE_NAME = re.compile(r"[a-z_]{1,24}")
_conn: Connection | None = None


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Report how long one step of the operation took; diagnostics only (#753).

    The parent adds it to the delivery timing line. Outside a worker (a direct
    call in a test or the offline check) nothing is sent. A step that raises is
    reported too (an absent partition is a manifest refusal); only a fixed name
    and a duration ever cross the pipe, and a report that cannot be sent never
    replaces the step's own outcome.
    """
    if _PHASE_NAME.fullmatch(name) is None:
        raise ValueError("worker phase names are fixed lowercase words")
    started = time.monotonic()
    try:
        yield
    finally:
        if _conn is not None:
            try:
                _conn.send(("phase", (name, (time.monotonic() - started) * 1000)))
            except OSError:
                pass


class DispatchExpired(BaseException):
    """The parent's grant expired before native I/O; never caught as an SDK error."""


def failure_code(exc: BaseException) -> str:
    """A bounded class for a native failure; the exception's message is never read.

    A native function may declare its own class through ``native_failure_code``
    (for example a provider API that answers with an auth or quota error).
    Otherwise the class comes from the exception types alone.
    """
    declared = getattr(exc, "native_failure_code", None)
    if isinstance(declared, str) and _FAILURE_CODE.fullmatch(declared):
        return declared
    if isinstance(exc, MemoryError):
        return "memory"
    names = {cls.__name__ for cls in type(exc).__mro__}
    if isinstance(exc, TimeoutError) or names & _TIMEOUT_NAMES:
        return "timeout"
    if "JSONDecodeError" in names:
        return "bad_response"
    if isinstance(exc, ConnectionError) or names & _NETWORK_NAMES:
        return "network"
    if isinstance(exc, OSError):
        return "os_error"
    return "sdk_error"


def _authorizer(conn: Connection, enabled: bool) -> Callable[[], None]:
    def authorize() -> None:
        if not enabled:
            return
        conn.send(("ready", None))
        deadline = conn.recv()
        # No child-side retry: expired IPC grants cannot multiply the parent's
        # bounded authorization attempts or permit delayed native dispatch.
        if time.monotonic() >= deadline:
            conn.send(("expired", None))
            raise DispatchExpired

    return authorize


def main() -> None:
    global _conn
    conn = _conn = Connection(int(sys.argv[1]))
    # ``python -m`` runs this file as ``__main__``; provider modules import
    # ``phase`` from the package module, which needs the same connection.
    setattr(importlib.import_module("findb_fetcher.provider_worker"), "_conn", conn)  # noqa: B010
    try:
        operation, payload, checkpoint = conn.recv()
        module, name = OPERATIONS[operation]
        function: Callable[[dict[str, Any], Callable[[], None]], Any] = getattr(
            importlib.import_module(module), name
        )
        conn.send(("result", function(payload, _authorizer(conn, checkpoint))))
    except DispatchExpired:
        pass
    except BaseException as exc:
        conn.send(("error", failure_code(exc)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
