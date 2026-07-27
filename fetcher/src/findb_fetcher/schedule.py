"""Strict configuration for the durable Fetcher scheduler."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

_MAX_CONFIG_BYTES = 64 * 1024
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_ROOT_KEYS = {
    "schedule_version",
    "schedule_id",
    "universe_file",
    "hour_utc",
    "minute_utc",
    "outputsize",
    "max_attempts",
    "retry_base_seconds",
    "retry_max_seconds",
    "lease_seconds",
    "poll_interval_seconds",
    "wait_timeout_seconds",
}


class ScheduleError(ValueError):
    """A scheduler configuration or clock value violates its safety contract."""


class _DuplicateJSONKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    schedule_version: int
    schedule_id: str
    universe_file: Path
    hour_utc: int
    minute_utc: int
    outputsize: int
    max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int
    lease_seconds: int
    poll_interval_seconds: int
    wait_timeout_seconds: int

    def latest_due_date(self, now: datetime) -> date:
        """Return the latest weekday UTC schedule date that is due at ``now``."""
        normalized = _utc_datetime(now)
        scheduled_today = datetime.combine(
            normalized.date(),
            time(self.hour_utc, self.minute_utc),
            tzinfo=timezone.utc,
        )
        candidate = (
            normalized.date() - timedelta(days=1)
            if normalized < scheduled_today
            else normalized.date()
        )
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate

    def retry_delay_seconds(self, attempt_count: int) -> int:
        if attempt_count < 1:
            raise ScheduleError("attempt_count must be at least 1")
        return min(self.retry_base_seconds * (2 ** (attempt_count - 1)), self.retry_max_seconds)


def load_schedule_config(path: Path) -> ScheduleConfig:
    """Load a strict scheduler config and resolve its universe beside the config."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ScheduleError(f"unable to read schedule config: {path}") from exc
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ScheduleError("schedule config exceeds size limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKeyError) as exc:
        raise ScheduleError("schedule config must be valid UTF-8 JSON with unique keys") from exc
    if not isinstance(value, dict) or set(value) != _ROOT_KEYS:
        raise ScheduleError(f"schedule config keys must be exactly {sorted(_ROOT_KEYS)}")

    schedule_version = _bounded_int(value, "schedule_version", 1, 1)
    schedule_id = _required_string(value, "schedule_id")
    if _IDENTIFIER_PATTERN.fullmatch(schedule_id) is None:
        raise ScheduleError("schedule_id must be a stable lowercase identifier")
    universe_name = _required_string(value, "universe_file")
    universe_path = Path(universe_name)
    if (
        universe_path.is_absolute()
        or len(universe_path.parts) != 1
        or universe_path.name != universe_name
        or universe_path.suffix != ".json"
    ):
        raise ScheduleError("universe_file must be a JSON filename beside the schedule config")

    config = ScheduleConfig(
        schedule_version=schedule_version,
        schedule_id=schedule_id,
        universe_file=path.parent / universe_path,
        hour_utc=_bounded_int(value, "hour_utc", 0, 23),
        minute_utc=_bounded_int(value, "minute_utc", 0, 59),
        outputsize=_bounded_int(value, "outputsize", 1, 5000),
        max_attempts=_bounded_int(value, "max_attempts", 1, 10),
        retry_base_seconds=_bounded_int(value, "retry_base_seconds", 1, 3600),
        retry_max_seconds=_bounded_int(value, "retry_max_seconds", 1, 86_400),
        lease_seconds=_bounded_int(value, "lease_seconds", 60, 28_800),
        poll_interval_seconds=_bounded_int(value, "poll_interval_seconds", 1, 3600),
        wait_timeout_seconds=_bounded_int(value, "wait_timeout_seconds", 1, 7200),
    )
    if config.retry_base_seconds > config.retry_max_seconds:
        raise ScheduleError("retry_base_seconds must not exceed retry_max_seconds")
    if config.lease_seconds <= config.wait_timeout_seconds:
        raise ScheduleError("lease_seconds must exceed wait_timeout_seconds")
    return config


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleError("scheduler clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _required_string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError(f"{key} must be a non-empty string")
    return value.strip()


def _bounded_int(parent: dict[str, Any], key: str, lower: int, upper: int) -> int:
    value = parent.get(key)
    if type(value) is not int or value < lower or value > upper:
        raise ScheduleError(f"{key} must be an integer between {lower} and {upper}")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(key)
        result[key] = value
    return result
