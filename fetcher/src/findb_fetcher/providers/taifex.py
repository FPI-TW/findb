"""TAIFEX actual monthly/weekly contract EOD with exchange-attributed sessions."""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from findb_fetcher.full_market_universe import (
    TAIFEX_HISTORY_URL,
    TAIFEX_URL,
    UniverseSnapshotError,
    _member,
)
from findb_fetcher.http_response import BoundedResponseError, read_identity_response

_PRODUCTS = {"TX", "MTX", "TMF", "TE", "TF"}
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


def parse_report(
    raw: bytes, *, target_date: date, historical: bool = False
) -> list[dict[str, Any]]:
    if historical:
        text = raw.decode("cp950").lstrip("\ufeff")
        source_rows = [
            {str(key).strip(): value for key, value in row.items() if key is not None}
            for row in csv.DictReader(io.StringIO(text))
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
    with client.stream(method, url, data=data, headers={"Accept-Encoding": "identity"}) as response:
        if response.status_code == 429:
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
                raise UniverseSnapshotError("TAIFEX report request failed")
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
            raise UniverseSnapshotError(f"TAIFEX report {exc}") from exc
        except httpx.HTTPError as exc:
            observed = getattr(exc, "observed_bytes", 0)
            if permit:
                permit.observe(observed)
            if on_observed_bytes is not None and observed:
                on_observed_bytes(observed)
            raise
