"""Exec'd worker for one allowlisted blocking provider operation.

``provider_process.supervised_call`` starts this module with a private pipe and
the inherited whole-work descriptor. The operation name selects a fixed
module-level function; no code, import path or callable crosses the pipe.
"""

from __future__ import annotations

import importlib
import sys
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

OPERATIONS = {
    "finlab.get": ("findb_fetcher.providers.finlab", "native_get"),
    "factset.query": ("findb_fetcher.providers.factset", "native_query"),
}


class DispatchExpired(BaseException):
    """The parent's grant expired before native I/O; never caught as an SDK error."""


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
    except BaseException:
        conn.send(("error", None))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
