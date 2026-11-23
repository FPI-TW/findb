"""Read accepted Source runs back behind later deliveries, never one at a time.

Per-member EOD deliveries (member historical dates, FactSet) leave each accepted
run unread while the next members are acquired and delivered, so normalization
overlaps acquisition. The backlog of unread runs is bounded, which keeps a
deferred date or work cheap to resume, and a run still queued is polled with
short growing delays instead of a fixed interval.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from uuid import UUID

import httpx

from findb_fetcher import rpc_timing
from findb_fetcher.client import SourceAPIResponseError, SourceAPITransportError
from findb_fetcher.historical_backfill import HistoricalWorkDeferredError

# Accepted runs left unread while later members are acquired and delivered.
UNCONFIRMED_WINDOW = 16
# An accepted run still queued is polled again after these delays, the last one repeating.
CONFIRM_DELAYS = (0.25, 0.5, 1.0, 2.0)
# A run not terminal after this long defers the date; settlement drains it later.
CONFIRM_TIMEOUT_SECONDS = 30
_PENDING = frozenset({"queued", "pending", "processing", "retrying"})


def terminal_run(delivery: Any, run_id: UUID, expected: Any, *, failed: threading.Event) -> Any:
    """The accepted run's terminal status, waiting only while it is still queued."""
    deadline = time.monotonic() + CONFIRM_TIMEOUT_SECONDS
    polls = 0
    while True:
        try:
            terminal = delivery.get_run_status(run_id, expected=expected)
        except (SourceAPITransportError, httpx.TransportError):
            raise HistoricalWorkDeferredError("Source status unavailable") from None
        except SourceAPIResponseError as exc:
            if exc.status_code >= 500 or exc.status_code in {408, 429}:
                raise HistoricalWorkDeferredError("Source status unavailable") from None
            raise
        if terminal.status not in _PENDING:
            return terminal
        if time.monotonic() >= deadline or failed.is_set():
            # Settlement drains accepted runs before releasing ownership.
            raise HistoricalWorkDeferredError("EOD normalization pending")
        with rpc_timing.timed("step normalization_wait"):
            time.sleep(CONFIRM_DELAYS[min(polls, len(CONFIRM_DELAYS) - 1)])
        polls += 1
