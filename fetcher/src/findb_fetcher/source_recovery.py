"""Retry idle Source failures without weakening active provider ownership."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from math import isfinite

import httpx

_STOP: ContextVar[threading.Event | None] = ContextVar("source_stop", default=None)


class SourceWaitStoppedError(Exception):
    """An idle Source wait was interrupted by graceful shutdown."""


def transient_source_status(status: int | None) -> bool:
    return status in {408, 429} or (status is not None and 500 <= status < 600)


def transient_source_error(error: BaseException) -> bool:
    from findb_fetcher.client import SourceAPIResponseError, SourceAPITransportError
    from findb_fetcher.full_market_client import FullMarketProtocolError
    from findb_fetcher.historical_backfill import HistoricalControlError
    from findb_fetcher.scheduler_control import (
        SchedulerControlResponseError,
        SchedulerControlTransportError,
    )

    if isinstance(error, HistoricalControlError):
        return (
            error.retryable
            or transient_source_status(error.status_code)
            or (error.status_code is None and isinstance(error.__cause__, httpx.TransportError))
        )
    if isinstance(error, FullMarketProtocolError):
        return error.retryable or transient_source_status(error.status_code)
    if isinstance(error, (SchedulerControlResponseError, SourceAPIResponseError)):
        return transient_source_status(error.status_code)
    return isinstance(
        error, (SchedulerControlTransportError, SourceAPITransportError, httpx.TransportError)
    )


@contextmanager
def source_stop_scope(stopped: threading.Event) -> Iterator[None]:
    token = _STOP.set(stopped)
    try:
        yield
    finally:
        _STOP.reset(token)


def source_retry_sleep(seconds: float, sleep: Callable[[float], None]) -> None:
    from findb_fetcher.provider_work import active

    stopped = _STOP.get()
    # Signals stop idle polling promptly. An owned work must still finish its
    # bounded acceptance/drain, or escape through the existing recovery fence.
    if stopped is None or active() is not None:
        sleep(seconds)
    elif stopped.wait(seconds):
        raise SourceWaitStoppedError


def retry_source[T](
    operation: Callable[[], T],
    *,
    stopped: threading.Event,
    run_forever: bool,
    max_delay: float = 30,
    on_retry: Callable[[], None] | None = None,
) -> T:
    delay = min(1.0, max_delay)
    while not stopped.is_set():
        try:
            return operation()
        except Exception as exc:
            if not run_forever or not transient_source_error(exc):
                raise
            if on_retry is not None:
                on_retry()
            # Never log a transport URL, arbitrary response, SDK detail or key.
            print("fetcher_source_wait reason=source_unavailable", file=sys.stderr, flush=True)
            retry_after = getattr(exc, "retry_after", 0)
            wait = max(delay, min(retry_after, max_delay)) if isfinite(retry_after) else delay
            if stopped.wait(wait):
                break
            delay = min(delay * 2, max_delay)
    raise SourceWaitStoppedError
