"""Strict configuration for the durable Fetcher scheduler."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_MAX_CONFIG_BYTES = 64 * 1024
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_V2_ROOT_KEYS = {"schedule_version", "timezone", "feeds"}
_V3_ROOT_KEYS = {"schedule_version", "deployment_target", "timezone", "feeds"}
_V2_FEED_KEYS = {
    "slot_id",
    "provider",
    "market",
    "dataset_key",
    "universe_file",
    "scheduled_time",
    "target_date_lag_days",
    "target_date_policy",
    "calendar_file",
    "grace_seconds",
    "outputsize",
    "max_attempts",
    "retry_base_seconds",
    "retry_max_seconds",
    "lease_seconds",
    "poll_interval_seconds",
    "wait_timeout_seconds",
    "max_credits_per_run",
    "max_records_per_run",
    "enabled",
}
_CALENDAR_ROOT_KEYS = {
    "calendar_version",
    "calendar_id",
    "market",
    "timezone",
    "coverage_start_date",
    "coverage_end_date",
    "non_trading_dates",
    "source_url",
    "reviewed_at",
}
# This allowlist intentionally has no clock/provider/dataset meaning.  The
# manifest carries the trigger time as data, so changing only ``scheduled_time``
# cannot change slot or schedule identity.
CANONICAL_SLOT_IDS = frozenset(
    {
        "western_markets_window",
        "global_markets_window",
        "taiwan_market_window",
        "asia_pacific_markets_window",
    }
)


class ScheduleError(ValueError):
    """A scheduler configuration or clock value violates its safety contract."""


class _DuplicateJSONKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _GovernedCalendar:
    calendar_id: str
    path: Path
    coverage_start_date: date
    coverage_end_date: date
    non_trading_dates: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    schedule_version: int
    schedule_id: str
    universe_file: Path
    outputsize: int
    max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int
    lease_seconds: int
    poll_interval_seconds: int
    wait_timeout_seconds: int
    slot_id: str
    provider: str
    market: str
    dataset_key: str
    timezone_name: str
    scheduled_local_time: time
    target_date_lag_days: int
    target_date_policy: str
    grace_seconds: int
    non_trading_dates: tuple[date, ...] = ()
    calendar_start_date: date | None = None
    calendar_end_date: date | None = None
    calendar_file: Path | None = None
    calendar_id: str | None = None
    max_credits_per_run: int = 5
    max_records_per_run: int = 10_000
    enabled: bool = True

    def retry_delay_seconds(self, attempt_count: int) -> int:
        if attempt_count < 1:
            raise ScheduleError("attempt_count must be at least 1")
        return min(self.retry_base_seconds * (2 ** (attempt_count - 1)), self.retry_max_seconds)

    def target_date(self, now: datetime) -> date:
        """Return the v2 target date; a trigger starts checking, not backfill."""
        candidate = self.scheduled_date(now)
        candidate -= timedelta(days=self.target_date_lag_days)
        if self.target_date_policy == "latest_trade_date" and (
            self.calendar_start_date is None
            or self.calendar_end_date is None
            or candidate < self.calendar_start_date
            or candidate > self.calendar_end_date
        ):
            raise ScheduleError("target date is outside the governed market calendar")
        while candidate.weekday() >= 5 or (
            self.target_date_policy == "latest_trade_date" and candidate in self.non_trading_dates
        ):
            candidate -= timedelta(days=1)
        if (
            self.target_date_policy == "latest_trade_date"
            and self.calendar_start_date is not None
            and candidate < self.calendar_start_date
        ):
            raise ScheduleError("target date is outside the governed market calendar")
        return candidate

    def scheduled_date(self, now: datetime) -> date:
        """Return the local calendar date of the latest due schedule trigger."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ScheduleError("scheduler clock must be timezone-aware")
        local = now.astimezone(ZoneInfo(self.timezone_name))
        trigger = datetime.combine(
            local.date(),
            self.scheduled_local_time,
            tzinfo=local.tzinfo,
        )
        candidate = local.date() if local >= trigger else local.date() - timedelta(days=1)
        return candidate

    def grace_deadline(self, scheduled_date: date) -> datetime:
        """Return the UTC deadline before a retryable miss may become terminal."""
        local = datetime.combine(
            scheduled_date,
            self.scheduled_local_time,
            tzinfo=ZoneInfo(self.timezone_name),
        )
        return (local + timedelta(seconds=self.grace_seconds)).astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ScheduleManifest:
    schedule_version: int
    timezone_name: str
    feeds: tuple[ScheduleConfig, ...]
    deployment_target: str = "staging"


def load_schedule_config(path: Path) -> ScheduleConfig:
    """Load a current v2 manifest containing exactly one feed."""
    manifest = load_schedule_manifest(path)
    if len(manifest.feeds) != 1:
        raise ScheduleError("v2 manifest has multiple feeds; use load_schedule_manifest")
    return manifest.feeds[0]


def load_schedule_manifest(path: Path) -> ScheduleManifest:
    """Load a target-bound schedule manifest and reject cross-target identity mixing."""
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ScheduleError("schedule config exceeds size limit")
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKeyError) as exc:
        raise ScheduleError("schedule config must be valid UTF-8 JSON with unique keys") from exc
    if not isinstance(value, dict):
        raise ScheduleError("schedule config must be an object")
    version = value.get("schedule_version")
    target = os.getenv("DEPLOYMENT_TARGET", "").strip().lower()
    if version == 2:
        if target == "production":
            raise ScheduleError("production target cannot load a staging v2 schedule")
        return _load_manifest(path, value, version=2, deployment_target="staging")
    if version == 3:
        configured_target = _required_string(value, "deployment_target")
        if configured_target != "production":
            raise ScheduleError("v3 deployment_target must be production")
        if target and target != configured_target:
            raise ScheduleError("schedule deployment_target does not match runtime target")
        return _load_manifest(path, value, version=3, deployment_target=configured_target)
    raise ScheduleError(f"v2 schedule keys must be exactly {sorted(_V2_ROOT_KEYS)}")


def _load_manifest(
    path: Path, value: dict[str, Any], *, version: int, deployment_target: str
) -> ScheduleManifest:
    root_keys = _V2_ROOT_KEYS if version == 2 else _V3_ROOT_KEYS
    if set(value) != root_keys or value.get("schedule_version") != version:
        raise ScheduleError(f"v{version} schedule keys must be exactly {sorted(root_keys)}")
    timezone_name = _required_string(value, "timezone")
    if timezone_name != "Asia/Taipei":
        raise ScheduleError("v2 scheduler timezone must be Asia/Taipei")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ScheduleError("scheduler timezone is invalid") from exc
    feeds_value = value.get("feeds")
    if not isinstance(feeds_value, list) or not feeds_value:
        raise ScheduleError("v2 feeds must be a non-empty list")
    feeds: list[ScheduleConfig] = []
    seen_feeds: set[tuple[str, str, str]] = set()
    for index, item in enumerate(feeds_value):
        if not isinstance(item, dict) or set(item) != _V2_FEED_KEYS:
            raise ScheduleError(f"feeds[{index}] keys must be exactly {sorted(_V2_FEED_KEYS)}")
        slot_id = _required_string(item, "slot_id")
        if slot_id not in CANONICAL_SLOT_IDS:
            raise ScheduleError("slot_id must be one of the four v2 slots")
        scheduled_local_time = _parse_scheduled_time(item.get("scheduled_time"))
        provider = _identifier(item, "provider")
        if provider not in {"twelve_data", "finlab"}:
            raise ScheduleError("provider is unsupported")
        dataset_key = _identifier(item, "dataset_key")
        feed_identity = (slot_id, provider, dataset_key)
        if feed_identity in seen_feeds:
            raise ScheduleError("v2 feed identity must be unique")
        seen_feeds.add(feed_identity)
        universe_file = _universe_path(path, _required_string(item, "universe_file"))
        policy = _required_string(item, "target_date_policy")
        if policy not in {"latest_weekday", "latest_trade_date"}:
            raise ScheduleError("target_date_policy is unsupported")
        calendar = _load_calendar(
            path,
            item.get("calendar_file"),
            feed_market=_required_string(item, "market"),
            timezone_name=timezone_name,
        )
        config = ScheduleConfig(
            schedule_version=version,
            schedule_id=f"{provider}_{slot_id}_{dataset_key}",
            universe_file=universe_file,
            outputsize=_bounded_int(item, "outputsize", 1, 5000),
            max_attempts=_bounded_int(item, "max_attempts", 1, 10),
            retry_base_seconds=_bounded_int(item, "retry_base_seconds", 1, 3600),
            retry_max_seconds=_bounded_int(item, "retry_max_seconds", 1, 86400),
            lease_seconds=_bounded_int(item, "lease_seconds", 60, 28800),
            poll_interval_seconds=_bounded_int(item, "poll_interval_seconds", 1, 3600),
            wait_timeout_seconds=_bounded_int(item, "wait_timeout_seconds", 1, 7200),
            scheduled_local_time=scheduled_local_time,
            target_date_lag_days=_bounded_int(item, "target_date_lag_days", 0, 366),
            slot_id=slot_id,
            provider=provider,
            market=_required_string(item, "market"),
            dataset_key=dataset_key,
            timezone_name=timezone_name,
            target_date_policy=policy,
            grace_seconds=_bounded_int(item, "grace_seconds", 0, 86400),
            non_trading_dates=() if calendar is None else calendar.non_trading_dates,
            calendar_start_date=None if calendar is None else calendar.coverage_start_date,
            calendar_end_date=None if calendar is None else calendar.coverage_end_date,
            calendar_file=None if calendar is None else calendar.path,
            calendar_id=None if calendar is None else calendar.calendar_id,
            max_credits_per_run=_bounded_int(item, "max_credits_per_run", 1, 100000),
            max_records_per_run=_bounded_int(item, "max_records_per_run", 1, 100000),
            enabled=item["enabled"],
        )
        if (
            config.retry_base_seconds > config.retry_max_seconds
            or config.lease_seconds <= config.wait_timeout_seconds
        ):
            raise ScheduleError("v2 retry or lease bounds are invalid")
        if config.enabled is not True and config.enabled is not False:
            raise ScheduleError("enabled must be a boolean")
        if config.target_date_policy == "latest_trade_date" and config.enabled and calendar is None:
            raise ScheduleError("enabled trade-date policy requires a governed calendar")
        feeds.append(config)
    return ScheduleManifest(version, timezone_name, tuple(feeds), deployment_target)


def _identifier(parent: dict[str, Any], key: str) -> str:
    value = _required_string(parent, key)
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ScheduleError(f"{key} must be a stable lowercase identifier")
    return value


def _universe_path(path: Path, universe_name: str) -> Path:
    universe_path = Path(universe_name)
    if (
        universe_path.is_absolute()
        or len(universe_path.parts) != 1
        or universe_path.name != universe_name
        or universe_path.suffix != ".json"
    ):
        raise ScheduleError("universe_file must be a JSON filename beside the schedule config")
    return path.parent / universe_path


def _load_calendar(
    manifest_path: Path,
    calendar_name: Any,
    *,
    feed_market: str,
    timezone_name: str,
) -> _GovernedCalendar | None:
    if calendar_name is None:
        return None
    if not isinstance(calendar_name, str) or not calendar_name.strip():
        raise ScheduleError("calendar_file must be a non-empty JSON path or null")
    calendar_path = _calendar_path(manifest_path, calendar_name.strip())
    try:
        raw = calendar_path.read_bytes()
    except OSError as exc:
        raise ScheduleError(f"unable to read governed calendar: {calendar_name}") from exc
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ScheduleError("governed calendar exceeds size limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKeyError) as exc:
        raise ScheduleError("governed calendar must be valid UTF-8 JSON with unique keys") from exc
    if not isinstance(value, dict) or set(value) != _CALENDAR_ROOT_KEYS:
        raise ScheduleError(f"governed calendar keys must be exactly {sorted(_CALENDAR_ROOT_KEYS)}")
    if _bounded_int(value, "calendar_version", 1, 1) != 1:
        raise ScheduleError("calendar_version must be 1")
    if _required_string(value, "market") != feed_market:
        raise ScheduleError("governed calendar market must match feed market")
    if _required_string(value, "timezone") != timezone_name:
        raise ScheduleError("governed calendar timezone must match manifest timezone")
    coverage_start_date = _required_date(value, "coverage_start_date")
    coverage_end_date = _required_date(value, "coverage_end_date")
    source_url = _required_string(value, "source_url")
    if not source_url.startswith("https://"):
        raise ScheduleError("governed calendar source_url must use HTTPS")
    _required_date(value, "reviewed_at")
    non_trading_dates = _date_list(value, "non_trading_dates")
    if coverage_start_date > coverage_end_date or any(
        holiday < coverage_start_date or holiday > coverage_end_date
        for holiday in non_trading_dates
    ):
        raise ScheduleError("governed calendar date range is invalid")
    return _GovernedCalendar(
        calendar_id=_identifier(value, "calendar_id"),
        path=calendar_path,
        coverage_start_date=coverage_start_date,
        coverage_end_date=coverage_end_date,
        non_trading_dates=non_trading_dates,
    )


def _calendar_path(manifest_path: Path, calendar_name: str) -> Path:
    calendar_path = Path(calendar_name)
    if (
        calendar_path.is_absolute()
        or calendar_path.suffix != ".json"
        or len(calendar_path.parts) != 2
        or calendar_path.parts[0] != "calendars"
        or calendar_path.name != calendar_name.split("/", maxsplit=1)[-1]
    ):
        raise ScheduleError("calendar_file must be a JSON file under configs/calendars")
    try:
        manifest_directory = manifest_path.parent.resolve(strict=True)
        governed_directory = manifest_directory / "calendars"
        resolved_calendar_path = (manifest_directory / calendar_path).resolve(strict=True)
        resolved_calendar_path.relative_to(governed_directory)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScheduleError(
            "calendar_file must resolve inside the governed configs/calendars directory"
        ) from exc
    return resolved_calendar_path


def _required_string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError(f"{key} must be a non-empty string")
    return value.strip()


def _parse_scheduled_time(value: object) -> time:
    """Parse an unzoned local wall-clock time carried by a v2 feed."""
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError("scheduled_time must be an HH:MM or HH:MM:SS time")
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError as exc:
        raise ScheduleError("scheduled_time must be an HH:MM or HH:MM:SS time") from exc
    if parsed.tzinfo is not None:
        raise ScheduleError("scheduled_time must not include a timezone")
    return parsed.replace(tzinfo=None)


def _bounded_int(parent: dict[str, Any], key: str, lower: int, upper: int) -> int:
    value = parent.get(key)
    if type(value) is not int or value < lower or value > upper:
        raise ScheduleError(f"{key} must be an integer between {lower} and {upper}")
    return value


def _date_list(parent: dict[str, Any], key: str) -> tuple[date, ...]:
    value = parent.get(key)
    if not isinstance(value, list):
        raise ScheduleError(f"{key} must be a list of ISO dates")
    try:
        parsed = tuple(date.fromisoformat(item) for item in value if isinstance(item, str))
    except ValueError as exc:
        raise ScheduleError(f"{key} must contain ISO dates") from exc
    if len(parsed) != len(value) or len(parsed) != len(set(parsed)):
        raise ScheduleError(f"{key} must contain unique ISO dates")
    return parsed


def _optional_date(parent: dict[str, Any], key: str) -> date | None:
    value = parent.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScheduleError(f"{key} must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ScheduleError(f"{key} must be an ISO date or null") from exc


def _required_date(parent: dict[str, Any], key: str) -> date:
    value = parent.get(key)
    if not isinstance(value, str):
        raise ScheduleError(f"{key} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ScheduleError(f"{key} must be an ISO date") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(key)
        result[key] = value
    return result
