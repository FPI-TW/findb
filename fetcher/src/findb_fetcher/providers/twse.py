"""TWSE (Taiwan Stock Exchange) public statistics.

``MI_5MINS_HIST`` serves one month of the TAIEX (發行量加權股價指數) daily open,
high, low and close per request, dated in the Minguo calendar. The current
day's row appears after the close; a row that is absent means not published.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

from findb_fetcher.provider_work import durable_response
from findb_fetcher.providers.tw_official import (
    TwOfficialFormatError,
    TwOfficialNotPublishedError,
    TwOfficialPayloadError,
    document,
    get,
    number,
    require_fields,
    roc_date,
)

TWSE_SOURCE = "twse"
TWSE_BASE_URL = "https://www.twse.com.tw"
TWSE_INDEX_PATH = "/rwd/zh/TAIEX/MI_5MINS_HIST"
TWSE_INDEX_FIELDS = ("日期", "開盤指數", "最高指數", "最低指數", "收盤指數")
# Journal identity of one month response read for a target day; never rename.
TWSE_INDEX_RESPONSE = "twse_index_month"
# TWSE's answer for a month without rows (a future month, or before the series).
_NO_ROWS = "很抱歉，沒有符合條件的資料!"


def index_month_url(month: date) -> str:
    """The credential-free URL of one month, used as evidence and raw identity."""
    return f"{TWSE_BASE_URL}{TWSE_INDEX_PATH}?date={month:%Y%m}01&response=json"


def index_rows(raw: bytes) -> dict[date, dict[str, Any]]:
    """Every day of one month response, keyed by date: open, high, low and close."""
    value = document(raw)
    stat = str(value.get("stat", "")).strip()
    if stat == _NO_ROWS:
        return {}
    if stat != "OK":
        raise TwOfficialPayloadError("TWSE index response is not OK")
    require_fields(value.get("fields"), TWSE_INDEX_FIELDS)
    rows = value.get("data")
    if not isinstance(rows, list):
        raise TwOfficialFormatError("TWSE index rows are not a list")
    days: dict[date, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != len(TWSE_INDEX_FIELDS):
            raise TwOfficialFormatError("TWSE index row has another shape")
        day = roc_date(row[0])
        if day in days:
            raise TwOfficialFormatError("TWSE index response repeats a date")
        days[day] = {
            "open": number(row[1]),
            "high": number(row[2]),
            "low": number(row[3]),
            "close": number(row[4]),
        }
    return days


def index_row(raw: bytes, day: date) -> dict[str, Any]:
    """The target day's levels; their absence means TWSE has not published the day."""
    days = index_rows(raw)
    if any((other.year, other.month) != (day.year, day.month) for other in days):
        raise TwOfficialFormatError("TWSE index response is of another month")
    if day not in days:
        raise TwOfficialNotPublishedError("twse_index_not_published")
    return days[day]


def _target_present(value: Any, arguments: dict[str, Any]) -> bool:
    """Replay a journaled month only when it already carried the target day."""
    try:
        index_row(value, arguments["day"])
    except (TwOfficialFormatError, TwOfficialNotPublishedError, TwOfficialPayloadError):
        return False
    except (TypeError, KeyError):
        return False
    return True


@durable_response(TWSE_INDEX_RESPONSE, ("day",), target_valid=_target_present)
def fetch_index_month(
    client: httpx.Client,
    *,
    day: date,
    max_response_bytes: int,
    on_http_rate_limited: Callable[[], None] | None = None,
    on_observed_bytes: Callable[[int], None] | None = None,
) -> bytes:
    """The exact bytes of the month that holds ``day``."""
    return get(
        TWSE_SOURCE,
        client,
        f"{TWSE_BASE_URL}{TWSE_INDEX_PATH}",
        {"date": f"{day:%Y%m}01", "response": "json"},
        max_response_bytes=max_response_bytes,
        on_http_rate_limited=on_http_rate_limited,
        on_observed_bytes=on_observed_bytes,
    )
