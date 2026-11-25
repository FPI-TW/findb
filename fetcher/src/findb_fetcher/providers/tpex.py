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
    integer,
    label,
    number,
    require_fields,
    roc_date,
    security_code,
    slash_date,
)

TPEX_SOURCE = "tpex"
TPEX_BASE_URL = "https://www.tpex.org.tw"
TPEX_INDEX_PATH = "/www/zh-tw/indexInfo/inx"
TPEX_INDEX_FIELDS = ("日期", "開市", "最高", "最低", "收市", "漲/跌")
# Journal identity of one month response read for a target day; never rename.
TPEX_INDEX_RESPONSE = "tpex_index_month"
# Three major institutional investors: market totals (TWD) and per security (shares).
TPEX_SUMMARY_PATH = "/www/zh-tw/insti/summary"
TPEX_SUMMARY_FIELDS = ("單位名稱", "買進金額(元)", "賣出金額(元)", "買賣超(元)")
TPEX_SUMMARY_RESPONSE = "tpex_institutional_summary"
TPEX_SUMMARY_LABELS = {
    "外資及陸資合計": "foreign_total",
    "外資及陸資(不含自營商)": "foreign_ex_dealer",
    "外資自營商": "foreign_dealer",
    "投信": "investment_trust",
    "自營商合計": "dealer_total",
    "自營商(自行買賣)": "dealer_proprietary",
    "自營商(避險)": "dealer_hedge",
    "三大法人合計*": "total",
}
TPEX_STOCK_PATH = "/www/zh-tw/insti/dailyTrade"
_SIDES = ("買進股數", "賣出股數", "買賣超股數")
# Seven groups of buy/sell/net, printed without their group names, then the total.
TPEX_STOCK_GROUPS = (
    "foreign_ex_dealer",
    "foreign_dealer",
    "foreign_total",
    "investment_trust",
    "dealer_proprietary",
    "dealer_hedge",
    "dealer_total",
)
TPEX_STOCK_FIELDS = ("代號", "名稱", *(_SIDES * len(TPEX_STOCK_GROUPS)), "三大法人買賣超股數合計")
TPEX_STOCK_RESPONSE = "tpex_institutional_stock"


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


def summary_url(day: date | None = None) -> str:
    """The credential-free URL of one day's totals; without a day, the latest one."""
    when = f"&date={day:%Y/%m/%d}" if day else ""
    return f"{TPEX_BASE_URL}{TPEX_SUMMARY_PATH}?type=Daily{when}&response=json"


def stock_url(day: date | None = None) -> str:
    when = f"&date={day:%Y/%m/%d}" if day else ""
    return f"{TPEX_BASE_URL}{TPEX_STOCK_PATH}?type=Daily&sect=AL{when}&response=json"


def _table(raw: bytes, day: date | None, fields: tuple[str, ...]) -> tuple[date, dict[str, Any]]:
    """The one daily table of a response and its date; empty tables beside it are ignored."""
    value = document(raw)
    if str(value.get("stat", "")).strip().lower() != "ok":
        raise TwOfficialPayloadError("TPEx table response is not ok")
    tables = value.get("tables")
    if not isinstance(tables, list) or not all(isinstance(table, dict) for table in tables):
        raise TwOfficialFormatError("TPEx table response has another layout")
    tables = [table for table in tables if table]
    if len(tables) != 1:
        raise TwOfficialFormatError("TPEx table response has another layout")
    table = tables[0]
    if not table.get("data"):
        # An unpublished day is answered with an empty table of that day.
        raise TwOfficialNotPublishedError("tpex_table_not_published")
    published = roc_date(table.get("date"))
    if day is not None and published != day:
        raise TwOfficialNotPublishedError("tpex_table_not_published")
    require_fields(table.get("fields"), fields)
    if any(not isinstance(row, list) or len(row) != len(fields) for row in table["data"]):
        raise TwOfficialFormatError("TPEx table row has another shape")
    return published, table


def summary_categories(raw: bytes, day: date | None) -> tuple[date, list[dict[str, Any]]]:
    """One day's totals by investor category, in TWD as printed, subtotals included."""
    published, table = _table(raw, day, TPEX_SUMMARY_FIELDS)
    categories: list[dict[str, Any]] = []
    for row in table["data"]:
        category = TPEX_SUMMARY_LABELS.get(label(row[0]))
        if category is None:
            raise TwOfficialFormatError("TPEx totals carry an unknown category")
        categories.append(
            {
                "category": category,
                "buy_amount": integer(row[1]),
                "sell_amount": integer(row[2]),
                "net_amount": integer(row[3]),
            }
        )
    if sorted(str(item["category"]) for item in categories) != sorted(TPEX_SUMMARY_LABELS.values()):
        raise TwOfficialFormatError("TPEx totals do not list each category once")
    return published, categories


def stock_rows(raw: bytes, day: date | None) -> tuple[date, list[dict[str, Any]]]:
    """One day's flows of every security in shares, warrants included."""
    published, table = _table(raw, day, TPEX_STOCK_FIELDS)
    if table.get("totalCount") != len(table["data"]):
        raise TwOfficialFormatError("TPEx per-security table is incomplete")
    result = []
    for row in table["data"]:
        values = [integer(value) for value in row[2:]]
        parsed: dict[str, Any] = {
            "symbol": security_code(row[0]),
            "name": str(row[1]).strip() or None,
        }
        for position, group in enumerate(TPEX_STOCK_GROUPS):
            for offset, side in enumerate(("buy", "sell", "net")):
                parsed[f"{group}_{side}"] = values[position * 3 + offset]
        parsed["total_net"] = values[-1]
        result.append(parsed)
    return published, result


def _day_published(parser):
    def valid(value: Any, arguments: dict[str, Any]) -> bool:
        """Replay a journaled table only when it is already the target day's."""
        try:
            parser(value, arguments["day"])
        except (TwOfficialFormatError, TwOfficialNotPublishedError, TwOfficialPayloadError):
            return False
        except (TypeError, KeyError):
            return False
        return True

    return valid


@durable_response(TPEX_SUMMARY_RESPONSE, ("day",), target_valid=_day_published(summary_categories))
def fetch_summary(
    client: httpx.Client,
    *,
    day: date | None,
    max_response_bytes: int,
    on_http_rate_limited: Callable[[], None] | None = None,
    on_observed_bytes: Callable[[int], None] | None = None,
) -> bytes:
    """One day's totals; ``day=None`` asks for the latest published day."""
    params = {"type": "Daily", "response": "json"}
    if day is not None:
        params["date"] = f"{day:%Y/%m/%d}"
    return get(
        TPEX_SOURCE,
        client,
        f"{TPEX_BASE_URL}{TPEX_SUMMARY_PATH}",
        params,
        max_response_bytes=max_response_bytes,
        on_http_rate_limited=on_http_rate_limited,
        on_observed_bytes=on_observed_bytes,
    )


@durable_response(TPEX_STOCK_RESPONSE, ("day",), target_valid=_day_published(stock_rows))
def fetch_stock(
    client: httpx.Client,
    *,
    day: date | None,
    max_response_bytes: int,
    on_http_rate_limited: Callable[[], None] | None = None,
    on_observed_bytes: Callable[[int], None] | None = None,
) -> bytes:
    """One day's per-security table; ``day=None`` asks for the latest published day."""
    params = {"type": "Daily", "sect": "AL", "response": "json"}
    if day is not None:
        params["date"] = f"{day:%Y/%m/%d}"
    return get(
        TPEX_SOURCE,
        client,
        f"{TPEX_BASE_URL}{TPEX_STOCK_PATH}",
        params,
        max_response_bytes=max_response_bytes,
        on_http_rate_limited=on_http_rate_limited,
        on_observed_bytes=on_observed_bytes,
    )
