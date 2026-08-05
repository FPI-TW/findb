"""Source-backed desired-state control for production scheduler loops.

The scheduler control endpoint is deliberately kept independent from the
Fetcher-owned SQLite state.  PostgreSQL is the durable source of truth for
whether a scheduler is allowed to start another provider cycle; a transport or
protocol failure therefore fails closed and leaves the container alive to poll
again later.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import time as clock_time
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from findb_fetcher.config import FetcherConfig

_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_ERROR_CHARS = 512
_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_SCHEDULER_KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,100}$")
_SCHEDULER_KEYS = frozenset(
    {
        "twelve_data_us_common_stocks_daily_v1",
        "finlab_tw_1430_tw_equity_eod",
        "shioaji_tw_pilot_v1",
    }
)
_CONTROL_VALUE = Literal["running", "stopped"]


class SchedulerControlError(RuntimeError):
    """Base class for bounded scheduler-control transport/protocol failures."""


class SchedulerControlResponseError(SchedulerControlError):
    """The control endpoint returned a non-success HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"scheduler control returned HTTP {status_code}")


class SchedulerControlProtocolError(SchedulerControlError):
    """The control endpoint violated the fixed public response contract."""


class SchedulerControlTransportError(SchedulerControlError):
    """Control transport retries were exhausted."""


@dataclass(frozen=True, slots=True)
class SchedulerControlResponse:
    """Validated desired state returned by one control poll."""

    scheduler_key: str
    provider: str
    dataset_keys: tuple[str, ...]
    slot_id: str
    scheduled_local_time: clock_time
    timezone: str
    desired_state: _CONTROL_VALUE
    revision: int
    server_time: datetime


def scheduler_control_keys() -> frozenset[str]:
    """Return the scheduler identities supported by the fixed backend API."""

    return _SCHEDULER_KEYS


class SchedulerControlClient:
    """Small, identity-safe HTTP client for the scheduler control endpoint."""

    def __init__(
        self,
        config: FetcherConfig,
        scheduler_key: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(scheduler_key, str) or _SCHEDULER_KEY_PATTERN.fullmatch(scheduler_key) is None:
            raise ValueError("scheduler control key is invalid")
        self._config = config
        self.scheduler_key = scheduler_key
        self._client = client or httpx.Client(timeout=config.request_timeout_seconds)
        self._owns_client = client is None
        self._sleep = sleep
        self._now = now

    def poll(
        self,
        *,
        observed_state: _CONTROL_VALUE,
        cycle_started_at: datetime | None = None,
        cycle_completed_at: datetime | None = None,
        last_error: str | None = None,
    ) -> SchedulerControlResponse:
        """Report scheduler state and return the authoritative desired state.

        The request is immutable across retry attempts.  Datetimes are encoded
        as UTC ``Z`` timestamps and the optional error is bounded before it is
        sent, so a provider traceback or credential cannot accidentally become
        an unbounded control-plane payload.
        """

        if observed_state not in ("running", "stopped"):
            raise ValueError("observed_state must be running or stopped")
        body_payload = {
            "observed_state": observed_state,
            "cycle_started_at": _encode_datetime(cycle_started_at),
            "cycle_completed_at": _encode_datetime(cycle_completed_at),
            "last_error": _bound_error(last_error) if last_error else None,
        }
        body = json.dumps(
            body_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload = self._request(body)
        if payload.get("success") is not True:
            raise SchedulerControlProtocolError("scheduler control acknowledgement was invalid")
        key = payload.get("scheduler_key")
        if key != self.scheduler_key:
            raise SchedulerControlProtocolError("scheduler control identity mismatch")
        desired = payload.get("desired_state")
        if desired not in ("running", "stopped"):
            raise SchedulerControlProtocolError("scheduler control desired state was invalid")
        revision = payload.get("revision")
        if type(revision) is not int or revision < 1:
            raise SchedulerControlProtocolError("scheduler control revision was invalid")
        provider = payload.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise SchedulerControlProtocolError("scheduler control provider was invalid")
        dataset_keys = _parse_dataset_keys(payload.get("dataset_keys"))
        slot_id = payload.get("slot_id")
        if not isinstance(slot_id, str) or not slot_id.strip():
            raise SchedulerControlProtocolError("scheduler control slot_id was invalid")
        scheduled_local_time = _parse_local_time(payload.get("scheduled_local_time"))
        timezone_name = payload.get("timezone")
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise SchedulerControlProtocolError("scheduler control timezone was invalid")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise SchedulerControlProtocolError("scheduler control timezone was invalid") from exc
        server_time = _parse_utc_datetime(payload.get("server_time"), "server_time")
        return SchedulerControlResponse(
            scheduler_key=self.scheduler_key,
            provider=provider,
            dataset_keys=dataset_keys,
            slot_id=slot_id,
            scheduled_local_time=scheduled_local_time,
            timezone=timezone_name,
            desired_state=desired,
            revision=revision,
            server_time=server_time,
        )

    # ``report`` reads naturally at call sites and is retained as an alias for
    # tests/integrations that describe this operation as a report rather than a
    # poll.
    report = poll

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SchedulerControlClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(self, body: bytes) -> dict[str, Any]:
        url = (
            f"{self._config.source_api_url.rstrip('/')}/api/v1/source/scheduler-controls/"
            f"{self.scheduler_key}/poll"
        )
        headers = {
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "X-API-Key": self._config.source_client_key,
        }
        for attempt in range(1, self._config.max_attempts + 1):
            request = self._client.build_request(
                "POST",
                url,
                content=body,
                headers=headers,
                timeout=self._config.request_timeout_seconds,
            )
            try:
                response = self._client.send(request, stream=True)
            except httpx.TransportError:
                if attempt == self._config.max_attempts:
                    raise SchedulerControlTransportError(
                        "scheduler control transport retries exhausted"
                    ) from None
                self._sleep(self._backoff_seconds(attempt))
                continue
            try:
                retry_delay = self._retry_delay(response, attempt)
                body_bytes = _read_bounded(response)
            except httpx.TransportError:
                if attempt == self._config.max_attempts:
                    raise SchedulerControlTransportError(
                        "scheduler control transport retries exhausted"
                    ) from None
                self._sleep(self._backoff_seconds(attempt))
                continue
            finally:
                response.close()

            if 200 <= response.status_code < 300:
                return _decode_object(body_bytes)
            if (
                response.status_code not in _RETRYABLE_STATUS_CODES
                or attempt == self._config.max_attempts
            ):
                raise SchedulerControlResponseError(response.status_code)
            self._sleep(retry_delay)
        raise AssertionError("scheduler control retry loop ended unexpectedly")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value is None:
            return self._backoff_seconds(attempt)
        try:
            seconds = float(value)
        except ValueError:
            seconds = -1.0
        if isfinite(seconds) and seconds >= 0:
            return min(seconds, self._config.max_retry_after_seconds)
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return self._backoff_seconds(attempt)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return min(
            max(0.0, (retry_at - self._now()).total_seconds()),
            self._config.max_retry_after_seconds,
        )

    def _backoff_seconds(self, attempt: int) -> float:
        return min(float(2 ** (attempt - 1)), self._config.max_retry_after_seconds)


class SchedulerControlLoop:
    """Run a DB-controlled scheduler without cancelling an active cycle."""

    def __init__(
        self,
        client: SchedulerControlClient,
        *,
        interval_seconds: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        definition_validator: Callable[[SchedulerControlResponse], None] | None = None,
    ) -> None:
        self.client = client
        configured_interval = client._config.scheduler_control_poll_seconds
        self.interval_seconds = _validate_interval(
            configured_interval if interval_seconds is None else interval_seconds
        )
        self._sleep = sleep
        self._now = now
        self._definition_validator = definition_validator

    def run(
        self,
        cycle: Callable[[], Any],
        *,
        stop_event: threading.Event | None = None,
    ) -> int:
        """Run until ``stop_event`` is set, keeping control failures bounded.

        ``cycle`` is constructed/invoked only after two successful polls: an
        idle report whose desired state is ``running`` and a pre-cycle report
        whose ``observed_state`` is ``running``.  This second report closes the
        race where an operator stops a scheduler while it is idle.
        """

        stopper = stop_event or threading.Event()
        pending_error: str | None = None
        completed_at: datetime | None = None
        while not stopper.is_set():
            try:
                desired = self.client.poll(
                    observed_state="stopped",
                    cycle_completed_at=completed_at,
                    last_error=pending_error,
                )
            except SchedulerControlError:
                self._wait(stopper)
                continue

            # A successful idle poll is also the point at which a previously
            # failed completion report has reached the server. Do not repeat
            # that completion on later idle heartbeats: the backend preserves
            # the latest cycle timestamps and error until a new successful
            # completion explicitly clears it.
            pending_error = None
            completed_at = None
            if desired.desired_state != "running":
                self._wait(stopper)
                continue

            started_at = _utc_now(self._now())
            try:
                preflight = self.client.poll(
                    observed_state="running",
                    cycle_started_at=started_at,
                    cycle_completed_at=None,
                    last_error=None,
                )
            except SchedulerControlError:
                self._wait(stopper)
                continue
            if preflight.desired_state != "running":
                # No provider/runtime factory has been touched yet.
                self._wait(stopper)
                continue

            if self._definition_validator is not None:
                try:
                    self._definition_validator(preflight)
                except Exception as exc:
                    # A local manifest/provider mapping drift is a safe setup
                    # failure.  Do not construct a provider or enter a cycle;
                    # leave a bounded diagnostic for the next control poll.
                    pending_error = _bound_error(f"definition drift: {type(exc).__name__}: {exc}")
                    self._wait(stopper)
                    continue

            heartbeat_stop = threading.Event()
            heartbeat = _Heartbeat(
                self.client,
                started_at=started_at,
                interval_seconds=self.interval_seconds,
                stop_event=heartbeat_stop,
                outer_stop_event=stopper,
            )
            heartbeat.start()
            cycle_error: str | None = None
            try:
                cycle()
            except Exception as exc:  # provider failures must not kill the loop
                cycle_error = _bound_error(f"{type(exc).__name__}: {exc}")
            finally:
                heartbeat_stop.set()
                heartbeat.join()

            completed_at = _utc_now(self._now())
            try:
                self.client.poll(
                    observed_state="stopped",
                    cycle_started_at=started_at,
                    cycle_completed_at=completed_at,
                    last_error=cycle_error,
                )
            except SchedulerControlError:
                # Keep the error for the next bounded report.  A successful
                # idle poll clears it above; either way the provider cycle is
                # never started again without a fresh running acknowledgement.
                pending_error = cycle_error
                self._wait(stopper)
                continue
            pending_error = None
            completed_at = None
            self._wait(stopper)
        return 0

    run_forever = run

    def _wait(self, stopper: threading.Event) -> None:
        if not stopper.is_set():
            # Event-aware waiting lets tests and signal handlers stop promptly
            # while preserving the injectable sleep hook for deterministic
            # control-loop tests.
            if self._sleep is time.sleep:
                stopper.wait(self.interval_seconds)
            else:
                self._sleep(self.interval_seconds)


class _Heartbeat:
    def __init__(
        self,
        client: SchedulerControlClient,
        *,
        started_at: datetime,
        interval_seconds: float,
        stop_event: threading.Event,
        outer_stop_event: threading.Event,
    ) -> None:
        self._client = client
        self._started_at = started_at
        self._interval_seconds = interval_seconds
        self._stop_event = stop_event
        self._outer_stop_event = outer_stop_event
        self._thread = threading.Thread(
            target=self._run,
            name=f"scheduler-control-heartbeat-{client.scheduler_key}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            if self._outer_stop_event.is_set():
                return
            try:
                # A stop response is intentionally only observed.  The
                # provider cycle is synchronous and must finish gracefully;
                # the post-cycle stopped report prevents another cycle.
                self._client.poll(
                    observed_state="running",
                    cycle_started_at=self._started_at,
                    cycle_completed_at=None,
                    last_error=None,
                )
            except SchedulerControlError:
                continue


def _validate_interval(value: float) -> float:
    if not isinstance(value, (int, float)) or not isfinite(float(value)):
        raise ValueError("scheduler control interval must be finite")
    value = float(value)
    # Runtime environment parsing applies the stricter one-second floor.  A
    # small injectable floor here keeps deterministic heartbeat tests practical
    # without permitting a zero/negative busy loop in production callers that
    # construct a config object directly.
    if not 0.01 <= value <= 30.0:
        raise ValueError("scheduler control interval is outside bounds")
    return value


def _encode_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = _utc_now(value)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc_now(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler control datetimes must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_dataset_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SchedulerControlProtocolError("scheduler control dataset_keys was invalid")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise SchedulerControlProtocolError("scheduler control dataset_keys was invalid")
    if len(set(value)) != len(value):
        raise SchedulerControlProtocolError("scheduler control dataset_keys was duplicated")
    return tuple(value)


def _parse_local_time(value: object) -> clock_time:
    if not isinstance(value, str):
        raise SchedulerControlProtocolError("scheduler control scheduled_local_time was invalid")
    for format_value in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(value, format_value).time()
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed
    raise SchedulerControlProtocolError("scheduler control scheduled_local_time was invalid")


def validate_scheduler_definition(
    response: SchedulerControlResponse,
    *,
    provider: str,
    dataset_keys: tuple[str, ...] | list[str],
    slot_id: str,
    scheduled_local_time: clock_time | str,
    timezone_name: str,
) -> None:
    """Fail closed when local execution config drifts from DB definition."""
    expected_time = (
        _parse_local_time(scheduled_local_time)
        if isinstance(scheduled_local_time, str)
        else scheduled_local_time
    )
    if response.provider != provider:
        raise SchedulerControlProtocolError("scheduler control provider drifted")
    if tuple(sorted(response.dataset_keys)) != tuple(sorted(dataset_keys)):
        raise SchedulerControlProtocolError("scheduler control dataset mapping drifted")
    if response.slot_id != slot_id:
        raise SchedulerControlProtocolError("scheduler control slot drifted")
    if response.scheduled_local_time != expected_time:
        raise SchedulerControlProtocolError("scheduler control scheduled time drifted")
    if response.timezone != timezone_name:
        raise SchedulerControlProtocolError("scheduler control timezone drifted")


def _parse_utc_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise SchedulerControlProtocolError(f"scheduler control {field} was invalid")
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SchedulerControlProtocolError(f"scheduler control {field} was invalid") from exc
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise SchedulerControlProtocolError(f"scheduler control {field} must be UTC")
    return parsed.astimezone(timezone.utc)


def _read_bounded(response: httpx.Response) -> bytes:
    content_encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise SchedulerControlProtocolError("scheduler control response encoding was not allowed")
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise SchedulerControlProtocolError(
                "scheduler control Content-Length was invalid"
            ) from exc
        if declared < 0 or declared > _MAX_RESPONSE_BYTES:
            raise SchedulerControlProtocolError("scheduler control response exceeded size limit")
    body = bytearray()
    try:
        chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
        for chunk in chunks:
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise SchedulerControlProtocolError(
                    "scheduler control response exceeded size limit"
                )
    except httpx.StreamError as exc:
        raise SchedulerControlProtocolError("scheduler control response stream failed") from exc
    return bytes(body)


def _decode_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchedulerControlProtocolError(
            "scheduler control response was not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise SchedulerControlProtocolError("scheduler control response was not an object")
    return value


_SECRET_PATTERN = re.compile(
    r"(?i)\b(secret[_-]?access[_-]?key|access[_-]?key|api[_-]?key|secret|token|password|authorization|credential|key)\b\s*([:=])\s*[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def _bound_error(value: str | None) -> str | None:
    if not value:
        return None
    clean = " ".join(str(value).split())
    clean = _BEARER_PATTERN.sub("Bearer <redacted>", clean)
    clean = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", clean)
    return clean[:_MAX_ERROR_CHARS]


__all__ = [
    "SchedulerControlClient",
    "SchedulerControlError",
    "SchedulerControlLoop",
    "SchedulerControlProtocolError",
    "SchedulerControlResponse",
    "SchedulerControlResponseError",
    "SchedulerControlTransportError",
    "scheduler_control_keys",
    "validate_scheduler_definition",
]
