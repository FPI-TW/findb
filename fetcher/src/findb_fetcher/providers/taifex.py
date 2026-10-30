"""TAIFEX actual monthly/weekly contract EOD with exchange-attributed sessions."""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any

import httpx

from findb_fetcher.full_market_universe import (
    TAIFEX_HISTORY_URL,
    TAIFEX_URL,
    UniverseSnapshotError,
    _member,
)
from findb_fetcher.http_response import BoundedResponseError, read_identity_response
from findb_fetcher.provider_work import durable_response

PRODUCTS = ("TX", "MTX", "TMF", "TE", "TF")
_PRODUCTS = set(PRODUCTS)


class TaifexOperationalError(RuntimeError):
    """Transport, HTTP or response-bound failures must stop acquisition."""

    def __init__(
        self, message: str, *, status_code: int | None = None, retry_after: float = 0
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status_code == 429 or (self.status_code is not None and self.status_code >= 500)


def _retry_after(value: str | None) -> float:
    if value is None:
        return 0
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = retry_at.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, delay) if isfinite(delay) else 0


_FIELDS = {
    "Date": "交易日期",
    "Contract": "契約",
    "ContractMonth(Week)": "到期月份(週別)",
    "Open": "開盤價",
    "High": "最高價",
    "Low": "最低價",
    "Last": "收盤價",
    "Volume": "成交量",
    "SettlementPrice": "結算價",
    "OpenInterest": "未沖銷契約數",
    "TradingSession": "交易時段",
}


def _value(row: Mapping[str, Any], key: str) -> str:
    return str(row.get(key, row.get(_FIELDS[key], ""))).strip()


def _numeric(value: str, *, integer: bool = False) -> str | int | None:
    if value.upper() in {"", "-", "--", "---", "NULL"}:
        return None
    try:
        number = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise UniverseSnapshotError("TAIFEX numeric field is invalid") from exc
    if not number.is_finite() or number < 0 or (integer and number != number.to_integral_value()):
        raise UniverseSnapshotError("TAIFEX numeric field is invalid")
    return int(number) if integer else format(number, "f")


def official_report_date(raw: bytes) -> date:
    """Validate the official JSON envelope before deriving its attribution date."""
    try:
        values = json.loads(raw)
    except (ValueError, UnicodeError):
        raise UniverseSnapshotError("TAIFEX official report JSON is invalid") from None
    if not isinstance(values, list) or not values:
        raise UniverseSnapshotError("TAIFEX official report rows are absent")
    dates = set()
    for row in values:
        if not isinstance(row, dict) or not isinstance(row.get("Date"), str):
            raise UniverseSnapshotError("TAIFEX official report row/date is invalid")
        text = row["Date"].strip().replace("/", "").replace("-", "")
        if not re.fullmatch(r"[0-9]{8}", text):
            raise UniverseSnapshotError("TAIFEX official report date is invalid")
        try:
            dates.add(datetime.strptime(text, "%Y%m%d").date())
        except ValueError:
            raise UniverseSnapshotError("TAIFEX official report date is invalid") from None
    if len(dates) != 1:
        raise UniverseSnapshotError("TAIFEX official report dates differ")
    return dates.pop()


def parse_report(
    raw: bytes, *, target_date: date, historical: bool = False
) -> list[dict[str, Any]]:
    if historical:
        text = raw.decode("cp950").lstrip("\ufeff")
        reader = csv.DictReader(io.StringIO(text))
        columns = {str(key).strip() for key in reader.fieldnames or []}
        if any(
            english not in columns and chinese not in columns
            for english, chinese in _FIELDS.items()
        ):
            raise UniverseSnapshotError("TAIFEX historical report columns changed")
        source_rows = [
            {str(key).strip(): value for key, value in row.items() if key is not None}
            for row in reader
        ]
    else:
        source_rows = json.loads(raw)
    if not isinstance(source_rows, list) or not source_rows:
        raise UniverseSnapshotError("TAIFEX report is empty; no-trade requires source evidence")
    result = []
    seen = set()
    for source in source_rows:
        if not isinstance(source, dict):
            raise UniverseSnapshotError("TAIFEX report row is invalid")
        product = _value(source, "Contract")
        month = _value(source, "ContractMonth(Week)").replace(" ", "")
        if product not in _PRODUCTS or "/" in month:
            continue
        if not re.fullmatch(r"[0-9]{6}([WF][1-5])?", month):
            raise UniverseSnapshotError("TAIFEX actual expiry identity is invalid")
        raw_date = _value(source, "Date").replace("/", "").replace("-", "")
        if raw_date != target_date.strftime("%Y%m%d"):
            raise UniverseSnapshotError("TAIFEX report date differs from exchange attribution date")
        session = {"一般": "regular", "盤後": "after_hours"}.get(_value(source, "TradingSession"))
        if session is None:
            raise UniverseSnapshotError("TAIFEX trading session is unknown")
        identity = (product, month, session)
        if identity in seen:
            raise UniverseSnapshotError("TAIFEX contract/session is duplicated")
        seen.add(identity)
        row: dict[str, Any] = {
            "product_code": product,
            "contract_code": f"{product}:{month}",
            "contract_month": month,
            "trade_date": target_date.isoformat(),
            "session": session,
        }
        for key, field in (
            ("Open", "open"),
            ("High", "high"),
            ("Low", "low"),
            ("Last", "close"),
            ("Volume", "volume"),
            ("SettlementPrice", "settlement_price"),
            ("OpenInterest", "open_interest"),
        ):
            row[field] = _numeric(_value(source, key), integer=key in {"Volume", "OpenInterest"})
        result.append(row)
    if not result:
        raise UniverseSnapshotError("TAIFEX report has no in-scope actual contracts")
    # The dated report is published in stages: after-hours rows (complete at
    # 05:00) appear before the regular session. A final report always carries
    # regular rows; closures yield a header-only report instead.
    if not any(row["session"] == "regular" for row in result):
        raise UniverseSnapshotError("TAIFEX report lacks regular-session rows; publication pending")
    return sorted(
        result, key=lambda row: (row["product_code"], row["contract_code"], row["session"])
    )


def report_members(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = row["contract_code"]
        if code not in grouped:
            member = _member(
                row["product_code"],
                "XTAF",
                "TWD",
                "future",
                "weekly" if re.search(r"[WF][1-5]$", row["contract_month"]) else "monthly",
            )
            member.update(
                {
                    "provider_symbol": code,
                    "contract_code": code,
                    "product_code": row["product_code"],
                    "contract_month": row["contract_month"],
                    "sessions": [],
                    "mapping_status": "mapped",
                }
            )
            grouped[code] = member
        grouped[code]["sessions"].append(row["session"])
    return list(grouped.values())


def _target_report_present(value: bytes, arguments: dict[str, Any]) -> bool:
    try:
        rows = parse_report(
            value, target_date=arguments["target_date"], historical=not arguments["latest"]
        )
        return arguments["latest"] or all(
            row["product_code"] == arguments["product"] for row in rows
        )
    except (ValueError, UnicodeError):
        return False


@durable_response(
    "taifex_report", ("target_date", "latest", "product"), target_valid=_target_report_present
)
def fetch_report(
    client: httpx.Client,
    *,
    target_date: date,
    latest: bool,
    product: str = "TX",
    max_response_bytes: int = 16 * 1024 * 1024,
    on_observed_bytes: Callable[[int], None] | None = None,
    on_declared_bytes: Callable[[int], None] | None = None,
    on_http_rate_limited: Callable[[], None] | None = None,
    on_bounded_response: Callable[[int, int], None] | None = None,
) -> bytes:
    if type(max_response_bytes) is not int or max_response_bytes < 1:
        raise ValueError("TAIFEX response byte bound invalid")
    from findb_fetcher.account_governor import provider_permit
    from findb_fetcher.acquisition import http_acquisition

    permit = provider_permit("taifex")
    bound = min(
        max_response_bytes, 16 * 1024 * 1024, permit.bound if permit else max_response_bytes
    )
    method, url = ("GET", TAIFEX_URL) if latest else ("POST", TAIFEX_HISTORY_URL)
    data = (
        None
        if latest
        else {
            "down_type": "1",
            "commodity_id": product,
            "commodity_id2": "",
            "queryStartDate": target_date.strftime("%Y/%m/%d"),
            "queryEndDate": target_date.strftime("%Y/%m/%d"),
        }
    )
    with (
        http_acquisition("taifex", client, url=url) as (operation, extensions),
        client.stream(
            method, url, data=data, headers={"Accept-Encoding": "identity"}, extensions=extensions
        ) as response,
    ):
        if response.status_code == 429:
            operation.rate_limited(response.headers.get("Retry-After"))
            if permit:
                permit.state.rate_limited(
                    account=permit.account,
                    window=permit.window,
                    until=time.time() + 60,
                )
            if on_http_rate_limited is not None:
                on_http_rate_limited()
        try:
            raw = read_identity_response(response, bound=bound)
            if permit:
                permit.observe(len(raw))
            if on_observed_bytes is not None:
                on_observed_bytes(len(raw))
            if response.status_code != 200:
                raise TaifexOperationalError(
                    "TAIFEX report request failed",
                    status_code=response.status_code,
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
            return raw
        except BoundedResponseError as exc:
            if permit:
                permit.observe(exc.observed_bytes, declared=exc.declared_bytes)
            if on_bounded_response is not None:
                on_bounded_response(exc.observed_bytes, exc.declared_bytes)
            else:
                if on_declared_bytes is not None and exc.declared_bytes:
                    on_declared_bytes(exc.declared_bytes)
                if on_observed_bytes is not None and exc.observed_bytes:
                    on_observed_bytes(exc.observed_bytes)
            raise TaifexOperationalError("TAIFEX report response bound failed") from exc
        except httpx.HTTPError as exc:
            observed = getattr(exc, "observed_bytes", 0)
            if permit:
                permit.observe(observed)
            if on_observed_bytes is not None and observed:
                on_observed_bytes(observed)
            raise


def persist_dated_report(
    raw: bytes, *, target_date: date, product: str, persist: Callable[[bytes], Any]
) -> tuple[Any, list[dict[str, Any]]]:
    """Persist exact report bytes before parsing or validating dated membership."""
    stored = persist(raw)
    rows = parse_report(raw, target_date=target_date, historical=True)
    if product not in _PRODUCTS or any(row["product_code"] != product for row in rows):
        raise UniverseSnapshotError("TAIFEX product report differs from request")
    return stored, rows


def is_no_trade(row: Mapping[str, Any]) -> bool:
    return row["volume"] == 0 and all(
        row[field] is None
        for field in ("open", "high", "low", "close", "settlement_price", "open_interest")
    )
