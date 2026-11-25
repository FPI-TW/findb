"""TPEx (Taipei Exchange) public statistics.

``indexInfo/inx`` serves one month of the TPEx index (櫃買指數) daily open, high,
low and close per request. The table is labelled with its Minguo month
(``115/11``) and its rows are dated ``2026/11/24``. The current day's row appears
after the close; a row that is absent means not published.
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
    slash_date,
)

TPEX_SOURCE = "tpex"
TPEX_BASE_URL = "https://www.tpex.org.tw"
TPEX_INDEX_PATH = "/www/zh-tw/indexInfo/inx"
TPEX_INDEX_FIELDS = ("日期", "開市", "最高", "最低", "收市", "漲/跌")
# Journal identity of one month response read for a target day; never rename.
TPEX_INDEX_RESPONSE = "tpex_index_month"


def index_month_url(month: date) -> str:
    """The credential-free URL of one month, used as evidence and raw identity."""
    return f"{TPEX_BASE_URL}{TPEX_INDEX_PATH}?date={month:%Y/%m}/01&response=json"


def index_rows(raw: bytes, month: date) -> dict[date, dict[str, Any]]:
    """Every day of one month response, keyed by date: open, high, low and close."""
    value = document(raw)
    if str(value.get("stat", "")).strip().lower() != "ok":
        raise TwOfficialPayloadError("TPEx index response is not ok")
    tables = value.get("tables")
    if not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], dict):
        raise TwOfficialFormatError("TPEx index response has another table layout")
    table = tables[0]
    if str(table.get("date", "")).strip() != f"{month.year - 1911}/{month:%m}":
        raise TwOfficialFormatError("TPEx index response is of another month")
    rows = table.get("data")
    if not isinstance(rows, list):
        raise TwOfficialFormatError("TPEx index rows are not a list")
    if not rows:
        # A month without rows yet (its first trading day before the close).
        return {}
    require_fields(table.get("fields"), TPEX_INDEX_FIELDS)
    days: dict[date, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != len(TPEX_INDEX_FIELDS):
            raise TwOfficialFormatError("TPEx index row has another shape")
        day = slash_date(row[0])
        if day in days or (day.year, day.month) != (month.year, month.month):
            raise TwOfficialFormatError("TPEx index response repeats or misplaces a date")
        days[day] = {
            "open": number(row[1]),
            "high": number(row[2]),
            "low": number(row[3]),
            "close": number(row[4]),
        }
    return days


def index_row(raw: bytes, day: date) -> dict[str, Any]:
    """The target day's levels; their absence means TPEx has not published the day."""
    days = index_rows(raw, day)
    if day not in days:
        raise TwOfficialNotPublishedError("tpex_index_not_published")
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


@durable_response(TPEX_INDEX_RESPONSE, ("day",), target_valid=_target_present)
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
        TPEX_SOURCE,
        client,
        f"{TPEX_BASE_URL}{TPEX_INDEX_PATH}",
        {"date": f"{day:%Y/%m}/01", "response": "json"},
        max_response_bytes=max_response_bytes,
        on_http_rate_limited=on_http_rate_limited,
        on_observed_bytes=on_observed_bytes,
    )
