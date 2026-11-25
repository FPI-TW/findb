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
    integer,
    label,
    number,
    require_fields,
    roc_date,
    security_code,
)

TWSE_SOURCE = "twse"
TWSE_BASE_URL = "https://www.twse.com.tw"
TWSE_INDEX_PATH = "/rwd/zh/TAIEX/MI_5MINS_HIST"
TWSE_INDEX_FIELDS = ("日期", "開盤指數", "最高指數", "最低指數", "收盤指數")
# Journal identity of one month response read for a target day; never rename.
TWSE_INDEX_RESPONSE = "twse_index_month"
# TWSE's answer for a period without rows (not published yet, or before the series).
_NO_ROWS = "很抱歉，沒有符合條件的資料!"
# Three major institutional investors: market totals (TWD) and per security (shares).
TWSE_SUMMARY_PATH = "/rwd/zh/fund/BFI82U"
TWSE_SUMMARY_FIELDS = ("單位名稱", "買進金額", "賣出金額", "買賣差額")
TWSE_SUMMARY_RESPONSE = "twse_institutional_summary"
TWSE_SUMMARY_LABELS = {
    "自營商(自行買賣)": "dealer_proprietary",
    "自營商(避險)": "dealer_hedge",
    "投信": "investment_trust",
    "外資及陸資(不含外資自營商)": "foreign_ex_dealer",
    "外資自營商": "foreign_dealer",
    "合計": "total",
}
TWSE_STOCK_PATH = "/rwd/zh/fund/T86"
TWSE_STOCK_FIELDS = (
    "證券代號",
    "證券名稱",
    "外陸資買進股數(不含外資自營商)",
    "外陸資賣出股數(不含外資自營商)",
    "外陸資買賣超股數(不含外資自營商)",
    "外資自營商買進股數",
    "外資自營商賣出股數",
    "外資自營商買賣超股數",
    "投信買進股數",
    "投信賣出股數",
    "投信買賣超股數",
    "自營商買賣超股數",
    "自營商買進股數(自行買賣)",
    "自營商賣出股數(自行買賣)",
    "自營商買賣超股數(自行買賣)",
    "自營商買進股數(避險)",
    "自營商賣出股數(避險)",
    "自營商買賣超股數(避險)",
    "三大法人買賣超股數",
)
TWSE_STOCK_RESPONSE = "twse_institutional_stock"
# Every class, warrants (0999) included: the Owner keeps the whole table.
TWSE_STOCK_SELECT = "ALL"


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


def summary_url(day: date | None = None) -> str:
    """The credential-free URL of one day's totals; without a day, the latest one."""
    when = f"&dayDate={day:%Y%m%d}" if day else ""
    return f"{TWSE_BASE_URL}{TWSE_SUMMARY_PATH}?type=day{when}&response=json"


def stock_url(day: date | None = None) -> str:
    when = f"date={day:%Y%m%d}&" if day else ""
    return f"{TWSE_BASE_URL}{TWSE_STOCK_PATH}?{when}selectType={TWSE_STOCK_SELECT}&response=json"


def _table(raw: bytes, day: date | None, fields: tuple[str, ...]) -> tuple[date, list[Any]]:
    """The rows of one daily table and its date; ``day`` must be the table's date."""
    value = document(raw)
    stat = str(value.get("stat", "")).strip()
    if stat == _NO_ROWS:
        raise TwOfficialNotPublishedError("twse_table_not_published")
    if stat != "OK":
        raise TwOfficialPayloadError("TWSE table response is not OK")
    try:
        published = date(int(value["date"][:4]), int(value["date"][4:6]), int(value["date"][6:8]))
    except (KeyError, TypeError, ValueError):
        raise TwOfficialFormatError("TWSE table date is invalid") from None
    if day is not None and published != day:
        # TWSE answers an unpublished day with the latest one it has.
        raise TwOfficialNotPublishedError("twse_table_not_published")
    require_fields(value.get("fields"), fields)
    rows = value.get("data")
    if not isinstance(rows, list) or not rows:
        raise TwOfficialFormatError("TWSE table rows are absent")
    if any(not isinstance(row, list) or len(row) != len(fields) for row in rows):
        raise TwOfficialFormatError("TWSE table row has another shape")
    return published, rows


def summary_categories(raw: bytes, day: date | None) -> tuple[date, list[dict[str, Any]]]:
    """One day's totals by investor category, in TWD as printed."""
    published, rows = _table(raw, day, TWSE_SUMMARY_FIELDS)
    categories: list[dict[str, Any]] = []
    for row in rows:
        category = TWSE_SUMMARY_LABELS.get(label(row[0]))
        if category is None:
            raise TwOfficialFormatError("TWSE totals carry an unknown category")
        categories.append(
            {
                "category": category,
                "buy_amount": integer(row[1]),
                "sell_amount": integer(row[2]),
                "net_amount": integer(row[3]),
            }
        )
    if sorted(str(item["category"]) for item in categories) != sorted(TWSE_SUMMARY_LABELS.values()):
        raise TwOfficialFormatError("TWSE totals do not list each category once")
    return published, categories


def stock_rows(raw: bytes, day: date | None) -> tuple[date, list[dict[str, Any]]]:
    """One day's flows of every security in shares; TWSE prints no foreign total."""
    published, rows = _table(raw, day, TWSE_STOCK_FIELDS)
    result = []
    for row in rows:
        values = [integer(value) for value in row[2:]]
        result.append(
            {
                "symbol": security_code(row[0]),
                "name": str(row[1]).strip() or None,
                "foreign_ex_dealer_buy": values[0],
                "foreign_ex_dealer_sell": values[1],
                "foreign_ex_dealer_net": values[2],
                "foreign_dealer_buy": values[3],
                "foreign_dealer_sell": values[4],
                "foreign_dealer_net": values[5],
                "foreign_total_buy": None,
                "foreign_total_sell": None,
                "foreign_total_net": None,
                "investment_trust_buy": values[6],
                "investment_trust_sell": values[7],
                "investment_trust_net": values[8],
                "dealer_total_buy": None,
                "dealer_total_sell": None,
                "dealer_total_net": values[9],
                "dealer_proprietary_buy": values[10],
                "dealer_proprietary_sell": values[11],
                "dealer_proprietary_net": values[12],
                "dealer_hedge_buy": values[13],
                "dealer_hedge_sell": values[14],
                "dealer_hedge_net": values[15],
                "total_net": values[16],
            }
        )
    if int(document(raw).get("total", len(result))) != len(result):
        raise TwOfficialFormatError("TWSE per-security table is incomplete")
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


@durable_response(TWSE_SUMMARY_RESPONSE, ("day",), target_valid=_day_published(summary_categories))
def fetch_summary(
    client: httpx.Client,
    *,
    day: date | None,
    max_response_bytes: int,
    on_http_rate_limited: Callable[[], None] | None = None,
    on_observed_bytes: Callable[[int], None] | None = None,
) -> bytes:
    """One day's totals; ``day=None`` asks for the latest published day."""
    params = {"type": "day", "response": "json"}
    if day is not None:
        params["dayDate"] = f"{day:%Y%m%d}"
    return get(
        TWSE_SOURCE,
        client,
        f"{TWSE_BASE_URL}{TWSE_SUMMARY_PATH}",
        params,
        max_response_bytes=max_response_bytes,
        on_http_rate_limited=on_http_rate_limited,
        on_observed_bytes=on_observed_bytes,
    )


@durable_response(TWSE_STOCK_RESPONSE, ("day",), target_valid=_day_published(stock_rows))
def fetch_stock(
    client: httpx.Client,
    *,
    day: date | None,
    max_response_bytes: int,
    on_http_rate_limited: Callable[[], None] | None = None,
    on_observed_bytes: Callable[[int], None] | None = None,
) -> bytes:
    """One day's per-security table; ``day=None`` asks for the latest published day."""
    params = {"selectType": TWSE_STOCK_SELECT, "response": "json"}
    if day is not None:
        params["date"] = f"{day:%Y%m%d}"
    return get(
        TWSE_SOURCE,
        client,
        f"{TWSE_BASE_URL}{TWSE_STOCK_PATH}",
        params,
        max_response_bytes=max_response_bytes,
        on_http_rate_limited=on_http_rate_limited,
        on_observed_bytes=on_observed_bytes,
    )
