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
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

OPERATIONS = {
    "finlab.get": ("findb_fetcher.providers.finlab", "native_get"),
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
    conn = Connection(int(sys.argv[1]))
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
