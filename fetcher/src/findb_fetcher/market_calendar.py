"""Strict, bounded client for complete published FinDB market calendars."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from findb_fetcher.config import MarketCalendarConfig

_MAX_RESPONSE_BYTES = 512 * 1024
_DAY_STATUSES = frozenset({"open", "closed", "settlement_only"})
_MARKET_PATTERN = re.compile(r"^[A-Z0-9_]{1,10}$")


class MarketCalendarError(RuntimeError):
    """A remote calendar could not be trusted for scheduling."""


@dataclass(frozen=True, slots=True)
class CalendarDay:
    trade_date: date
    day_status: str
    is_open: bool


@dataclass(frozen=True, slots=True)
class PublishedCalendar:
    market: str
    year: int
    revision: int
    timezone_name: str
    days: tuple[CalendarDay, ...]

    def day(self, value: date) -> CalendarDay:
        if value.year != self.year:
            raise MarketCalendarError("calendar date is outside the published year")
        index = (value - date(self.year, 1, 1)).days
        if index < 0 or index >= len(self.days):
            raise MarketCalendarError("calendar date is missing from the published year")
        item = self.days[index]
        if item.trade_date != value:
            raise MarketCalendarError("calendar dates are not contiguous")
        return item


class PublishedCalendarClient:
    """Read complete published years and cache them by immutable revision."""

    def __init__(
        self,
        config: MarketCalendarConfig,
        *,
        client: httpx.Client | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if config.mode != "remote" or config.serve_base_url is None or config.api_key is None:
            raise ValueError("remote market calendar configuration is required")
        self._config = config
        self._base_url = config.serve_base_url
        self._api_key = config.api_key
        self._client = client or httpx.Client(timeout=config.request_timeout_seconds)
        self._owns_client = client is None
        self._monotonic = monotonic
        self._cache: dict[tuple[str, int], tuple[float, PublishedCalendar]] = {}

    def get_year(self, market: str, year: int) -> PublishedCalendar:
        normalized_market = _market(market)
        if year < 1970 or year > 2200:
            raise MarketCalendarError("calendar year is outside supported bounds")
        cache_key = (normalized_market, year)
        cached = self._cache.get(cache_key)
        now = self._monotonic()
        if cached is not None and now - cached[0] < self._config.cache_ttl_seconds:
            return cached[1]

        path = f"/api/v1/serve/calendar/years/{normalized_market}/{year}"
        try:
            with self._client.stream(
                "GET",
                f"{self._base_url}{path}",
                headers={
                    "X-API-Key": self._api_key,
                    "Accept": "application/json",
                },
            ) as response:
                if response.status_code != 200:
                    raise MarketCalendarError(f"calendar API returned HTTP {response.status_code}")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise MarketCalendarError("calendar API response exceeds size limit")
        except MarketCalendarError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise MarketCalendarError("calendar API request failed") from exc

        calendar = _parse_calendar(
            bytes(raw), expected_market=normalized_market, expected_year=year
        )
        self._cache[cache_key] = (now, calendar)
        return calendar

    def get_day(self, market: str, value: date) -> tuple[CalendarDay, int]:
        calendar = self.get_year(market, value.year)
        return calendar.day(value), calendar.revision

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PublishedCalendarClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _parse_calendar(
    raw: bytes,
    *,
    expected_market: str,
    expected_year: int,
) -> PublishedCalendar:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MarketCalendarError("calendar API returned invalid JSON") from exc
    if not isinstance(value, dict) or value.get("success") is not True:
        raise MarketCalendarError("calendar API envelope is invalid")
    data = value.get("data")
    if not isinstance(data, dict):
        raise MarketCalendarError("calendar API data is invalid")

    market = _required_string(data, "market")
    year = _required_integer(data, "year")
    revision = _required_integer(data, "revision")
    status = _required_string(data, "status")
    coverage_complete = data.get("coverage_complete")
    timezone_name = _required_string(data, "timezone")
    expected_days = _required_integer(data, "expected_days")
    actual_days = _required_integer(data, "actual_days")
    if market != expected_market or year != expected_year:
        raise MarketCalendarError("calendar API identity mismatch")
    if revision < 1 or status != "published" or coverage_complete is not True:
        raise MarketCalendarError("calendar is not a complete published revision")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise MarketCalendarError("calendar timezone is invalid") from exc

    first = date(year, 1, 1)
    last = date(year, 12, 31)
    required_count = (last - first).days + 1
    if expected_days != required_count or actual_days != required_count:
        raise MarketCalendarError("calendar coverage count is invalid")
    raw_days = data.get("days")
    if not isinstance(raw_days, list) or len(raw_days) != required_count:
        raise MarketCalendarError("calendar must contain every day in the year")

    days: list[CalendarDay] = []
    for index, raw_day in enumerate(raw_days):
        if not isinstance(raw_day, dict):
            raise MarketCalendarError("calendar day is invalid")
        raw_date = raw_day.get("date", raw_day.get("trade_date"))
        if not isinstance(raw_date, str):
            raise MarketCalendarError("calendar day date is invalid")
        try:
            trade_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise MarketCalendarError("calendar day date is invalid") from exc
        if trade_date != first + timedelta(days=index):
            raise MarketCalendarError("calendar dates are duplicated, missing, or unordered")
        day_status = raw_day.get("day_status")
        is_open = raw_day.get("is_open")
        if day_status not in _DAY_STATUSES or type(is_open) is not bool:
            raise MarketCalendarError("calendar day status is invalid")
        if is_open is not (day_status == "open"):
            raise MarketCalendarError("calendar day open flag is inconsistent")
        days.append(
            CalendarDay(
                trade_date=trade_date,
                day_status=day_status,
                is_open=is_open,
            )
        )
    return PublishedCalendar(
        market=market,
        year=year,
        revision=revision,
        timezone_name=timezone_name,
        days=tuple(days),
    )


def _market(value: str) -> str:
    normalized = value.strip().upper()
    if _MARKET_PATTERN.fullmatch(normalized) is None:
        raise MarketCalendarError("market is invalid")
    return normalized


def _required_string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value:
        raise MarketCalendarError(f"calendar {key} is invalid")
    return value


def _required_integer(parent: dict[str, Any], key: str) -> int:
    value = parent.get(key)
    if type(value) is not int:
        raise MarketCalendarError(f"calendar {key} is invalid")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value
