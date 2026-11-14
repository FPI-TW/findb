"""Per-batch Source RPC and member-step timing for the delivery health line.

Only route templates and fixed step names are recorded; identifiers, query
strings and bodies never enter the totals, so the health line stays bounded.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

_IDENTIFIER = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_lock = threading.Lock()
_totals: dict[str, list[float]] = {}


def route(method: str, path: str) -> str:
    """``POST provider-work/{id}/prepare`` for ``/api/v1/source/provider-work/<uuid>/prepare``."""
    path = path.split("?", 1)[0]
    if "/api/v1/source/" in path:
        path = path.split("/api/v1/source/", 1)[1]
    return f"{method} {_IDENTIFIER.sub('{id}', path).strip('/')}"


def record(name: str, elapsed_ms: float) -> None:
    with _lock:
        entry = _totals.setdefault(name, [0, 0.0])
        entry[0] += 1
        entry[1] += elapsed_ms


@contextmanager
def timed(name: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        record(name, (time.monotonic() - started) * 1000)


def drain() -> dict[str, list[float]]:
    """Return ``{name: [count, total_ms]}`` since the previous drain and reset."""
    with _lock:
        totals = {name: [int(count), round(ms, 1)] for name, (count, ms) in sorted(_totals.items())}
        _totals.clear()
    return totals
