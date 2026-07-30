"""Narrow, lazy Shioaji 1.7.1 boundary and Taiwan minute-bar adapter.

The SDK is intentionally imported only by ``ShioajiSdkGateway.fetch_kbars``.
No provider objects or credential-bearing exceptions cross this boundary.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from zoneinfo import ZoneInfo

SHIOAJI_SOURCE = "shioaji"
TAIPEI = ZoneInfo("Asia/Taipei")
MAX_SYMBOLS = 50
MAX_ROWS = 15_000
_SYMBOL_MAX = 50


class ShioajiError(RuntimeError):
    """Base class with deliberately credential-free messages."""


class ShioajiConfigError(ShioajiError):
    pass


class ShioajiSdkError(ShioajiError):
    pass


class ShioajiPayloadError(ShioajiError):
    pass


@dataclass(frozen=True, slots=True)
class ShioajiKbarsSnapshot:
    """SDK-free bars plus optional, sanitized provider byte counters."""

    kbars: Mapping[str, tuple[object, ...]]
    usage_bytes_before: int | None
    usage_bytes_after: int | None

    @property
    def usage_bytes_delta(self) -> int | None:
        if (
            self.usage_bytes_before is None
            or self.usage_bytes_after is None
            or self.usage_bytes_after < self.usage_bytes_before
        ):
            return None
        return self.usage_bytes_after - self.usage_bytes_before


class ShioajiGateway(Protocol):
    def fetch_kbars(self, symbol: str, target_date: date) -> ShioajiKbarsSnapshot: ...


@dataclass(frozen=True, slots=True)
class ShioajiSdkGateway:
    """Credential-safe production gateway for the exact reviewed SDK surface."""

    api_key: str = field(repr=False, compare=False)
    secret_key: str = field(repr=False, compare=False)
    simulation: bool = True
    _sdk: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.api_key.strip() or not self.secret_key.strip():
            raise ShioajiConfigError("Shioaji credentials are required")
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(self, "secret_key", self.secret_key.strip())

    def __repr__(self) -> str:
        return "ShioajiSdkGateway(api_key=<redacted>, secret_key=<redacted>, simulation=%r)" % (
            self.simulation,
        )

    @classmethod
    def from_env(cls, *, simulation: bool = True) -> "ShioajiSdkGateway":
        return cls(
            api_key=os.getenv("SHIOAJI_API_KEY", ""),
            secret_key=os.getenv("SHIOAJI_SECRET_KEY", ""),
            simulation=simulation,
        )

    def fetch_kbars(self, symbol: str, target_date: date) -> ShioajiKbarsSnapshot:
        _validate_symbol(symbol)
        if not isinstance(target_date, date) or isinstance(target_date, datetime):
            raise ShioajiConfigError("target date is invalid")
        try:
            if importlib.metadata.version("shioaji") != "1.7.1":
                raise ShioajiSdkError("shioaji SDK version is unsupported")
            sdk = self._sdk if self._sdk is not None else importlib.import_module("shioaji")
            factory = getattr(sdk, "Shioaji")
            api = factory(simulation=self.simulation)
        except Exception:
            raise ShioajiSdkError("shioaji login failed") from None
        try:
            api.login(api_key=self.api_key, secret_key=self.secret_key, subscribe_trade=False)
            usage_before = _usage_bytes(api)
            # Shioaji 1.7.1 exposes lower-case contract categories.  Keep this
            # exact reviewed path rather than falling back to deprecated
            # ``api.Contracts`` aliases.
            contract = api.contracts.stocks.get(symbol)
            if contract is None:
                raise ShioajiSdkError("shioaji contract unavailable")
            raw = api.kbars(contract, start=target_date.isoformat(), end=target_date.isoformat())
            usage_after = _usage_bytes(api)
            return ShioajiKbarsSnapshot(
                _plain_kbars(_kbars_mapping(raw)),
                usage_before,
                usage_after,
            )
        except ShioajiError:
            raise
        except Exception:
            raise ShioajiSdkError("shioaji kbars retrieval failed") from None
        finally:
            try:
                api.logout()
            except Exception:
                # A provider error is never allowed to expose account context.
                pass


def build_market_minute_request(
    kbars: Mapping[str, Sequence[object]],
    *,
    dataset_key: str,
    target_date: date,
    symbols: Sequence[str],
    fetched_at: datetime,
    usage_before_requests: int,
    usage_after_requests: int,
    snapshot_id: str,
    daily_update_id: str,
    universe_id: str,
) -> dict[str, Any]:
    """Map Shioaji Kbars to deterministic ``market_minute.v1``.

    ``ts`` is treated as Taipei wall-clock nanoseconds and as a right-labelled
    bar end.  Thus a 13:30 close-auction point maps to 13:29--13:30; this known
    semantic limitation is retained pending an explicit contract decision.
    """
    if not dataset_key or not dataset_key.isascii() or not dataset_key.replace("_", "").isalnum():
        raise ShioajiPayloadError("dataset key is invalid")
    if not isinstance(target_date, date) or isinstance(target_date, datetime):
        raise ShioajiPayloadError("target date is invalid")
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ShioajiPayloadError("fetched_at must be timezone-aware")
    ordered_symbols = tuple(symbols)
    if (
        not ordered_symbols
        or len(ordered_symbols) > MAX_SYMBOLS
        or len(set(ordered_symbols)) != len(ordered_symbols)
    ):
        raise ShioajiPayloadError("symbols are outside bounded scope")
    for symbol in ordered_symbols:
        _validate_symbol(symbol)
    if not all(
        type(value) is int and value >= 0 for value in (usage_before_requests, usage_after_requests)
    ):
        raise ShioajiPayloadError("usage snapshots must be non-negative integers")
    if usage_after_requests < usage_before_requests:
        raise ShioajiPayloadError("usage counter must be monotonic")

    columns = ("ts", "Open", "High", "Low", "Close", "Volume", "Amount")
    if not all(
        isinstance(value, str) and 1 <= len(value) <= 100
        for value in (snapshot_id, daily_update_id, universe_id)
    ):
        raise ShioajiPayloadError("stable batch identity is invalid")
    if not isinstance(kbars, Mapping) or any(key not in kbars for key in columns):
        raise ShioajiPayloadError("kbars columns are incomplete")
    if any(not isinstance(kbars[key], tuple) for key in columns):
        raise ShioajiPayloadError("kbars vectors must be normalized tuples")
    lengths = {len(kbars[key]) for key in columns}
    if len(lengths) != 1:
        raise ShioajiPayloadError("kbars columns have inconsistent lengths")
    count = lengths.pop()
    if count > MAX_ROWS:
        raise ShioajiPayloadError("kbars rows exceed bounded scope")

    if count == 0:
        raise ShioajiPayloadError("empty kbars requires calendar evidence")
    # The smoke path uses one symbol; production callers must invoke once per
    # symbol so every Kbar remains attributable without provider metadata.
    if len(ordered_symbols) != 1:
        raise ShioajiPayloadError("one symbol is required per kbars response")
    symbol = ordered_symbols[0]
    rows: list[dict[str, Any]] = []
    anomalies: list[dict[str, str]] = []
    seen_ends: set[str] = set()
    for index in range(count):
        end = _bar_end(kbars["ts"][index], index)
        if end.date() != target_date:
            raise ShioajiPayloadError("kbars timestamp falls outside target date")
        end_utc = _utc_text(end)
        if end_utc in seen_ends:
            raise ShioajiPayloadError("duplicate kbars timestamp")
        seen_ends.add(end_utc)
        start_utc = _utc_text(end - timedelta(minutes=1))
        open_value = _price(kbars["Open"][index], index)
        high_value = _price(kbars["High"][index], index)
        low_value = _price(kbars["Low"][index], index)
        close_value = _price(kbars["Close"][index], index)
        if high_value < max(open_value, low_value, close_value) or low_value > min(
            open_value, high_value, close_value
        ):
            raise ShioajiPayloadError("kbars OHLC bounds are invalid")
        row: dict[str, Any] = {
            "symbol": symbol,
            "source_symbol": symbol,
            "trade_date": target_date.isoformat(),
            "market_timezone": "Asia/Taipei",
            "bar_start_time": start_utc,
            "bar_end_time": end_utc,
            "signal_time": end_utc,
            "price_adjustment": "none",
            "trade_count": None,
            "open": _decimal_text(open_value),
            "high": _decimal_text(high_value),
            "low": _decimal_text(low_value),
            "close": _decimal_text(close_value),
        }
        for provider_field, contract_field, multiplier in (
            ("Volume", "volume", 1000),
            ("Amount", "turnover", 1),
        ):
            value = kbars[provider_field][index]
            converted = _non_negative(value)
            if converted is None:
                raw = _anomaly_value(value)
                anomalies.append(
                    {
                        "symbol": symbol,
                        "bar_start_time": start_utc,
                        "field": contract_field,
                        "raw_value": raw,
                        "raw_value_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                        "reason": "invalid_provider_numeric_value",
                    }
                )
                row[contract_field] = None
            elif contract_field == "volume":
                shares = converted * multiplier
                if shares != shares.to_integral_value():
                    raise ShioajiPayloadError("provider volume lots are not integral")
                row[contract_field] = int(shares)
            else:
                row[contract_field] = _decimal_text(converted)
        rows.append(row)
    rows.sort(key=lambda item: item["bar_end_time"])
    symbols_digest = hashlib.sha256("\n".join(sorted(ordered_symbols)).encode()).hexdigest()
    identity = json.dumps(
        {
            "data_date": target_date.isoformat(),
            "dataset_key": dataset_key,
            "sequence": 1,
            "snapshot_id": snapshot_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(identity).hexdigest()
    batch: dict[str, Any] = {
        "data_date": target_date.isoformat(),
        "delivery_mode": "sequenced_snapshot",
        "declared_record_count": len(rows),
        "coverage_start_date": target_date.isoformat(),
        "coverage_end_date": target_date.isoformat(),
        "sequence": 1,
        "sequence_count": 1,
        "snapshot_id": snapshot_id,
        "daily_update_id": daily_update_id,
        "universe_id": universe_id,
        "symbols_sha256": symbols_digest,
        "provider_usage_before": {"requests_used": usage_before_requests, "requests_limit": 50},
        "provider_usage_after": {"requests_used": usage_after_requests, "requests_limit": 50},
    }
    if anomalies:
        batch["anomalies"] = anomalies
    return {
        "dataset_key": dataset_key,
        "schema_id": "market_minute",
        "schema_version": 1,
        "source": SHIOAJI_SOURCE,
        "request_key": f"mmr:{digest}",
        "idempotency_key": f"mms:{digest}",
        "fetched_at": _utc_text(fetched_at),
        "payload": {
            "batch": batch,
            "data": rows,
            "symbol_statuses": [
                {
                    "symbol": symbol,
                    "outcome": "data" if rows else "expected_no_data",
                    "reason": None if rows else "no_kbars",
                }
            ],
        },
    }


def _validate_symbol(symbol: object) -> None:
    if (
        not isinstance(symbol, str)
        or not symbol
        or len(symbol) > _SYMBOL_MAX
        or not symbol.isascii()
        or not symbol.replace(".", "").replace("-", "").isalnum()
    ):
        raise ShioajiPayloadError("symbol is invalid")


def _bar_end(value: object, index: int) -> datetime:
    if type(value) is not int or value < 0:
        raise ShioajiPayloadError(f"kbars timestamp at index {index} is invalid")
    # Provider integers are nanoseconds interpreted as a local wall-clock epoch.
    seconds, nanos = divmod(value, 1_000_000_000)
    try:
        return datetime(1970, 1, 1, tzinfo=TAIPEI) + timedelta(
            seconds=seconds, microseconds=nanos // 1000
        )
    except OverflowError as exc:
        raise ShioajiPayloadError("kbars timestamp is out of range") from exc


def _price(value: object, index: int) -> Decimal:
    numeric = _non_negative(value)
    if numeric is None:
        raise ShioajiPayloadError(f"kbars price at index {index} is invalid")
    return numeric


def _non_negative(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return decimal if decimal.is_finite() and decimal >= 0 else None


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _anomaly_value(value: object) -> str:
    # Evidence is exact but bounded and contains no provider request/context.
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return "<non_primitive>"
    text = str(value)
    return text[:500] if text else "<empty>"


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _plain_kbars(raw: Mapping[str, object]) -> dict[str, tuple[object, ...]]:
    """Detach provider/numpy vectors into a bounded, SDK-free representation."""
    required = ("ts", "Open", "High", "Low", "Close", "Volume", "Amount")
    result: dict[str, tuple[object, ...]] = {}
    for name in required:
        vector = raw.get(name)
        if isinstance(vector, (str, bytes)):
            raise ShioajiSdkError("shioaji kbars vectors are invalid")
        if not isinstance(vector, Iterable):
            raise ShioajiSdkError("shioaji kbars vectors are invalid")
        try:
            values: tuple[object, ...] = tuple(vector)  # numpy arrays detach here.
        except TypeError as exc:
            raise ShioajiSdkError("shioaji kbars vectors are invalid") from exc
        if len(values) > MAX_ROWS:
            raise ShioajiSdkError("shioaji kbars exceeds bounded rows")
        result[name] = tuple(_normalize_scalar(value) for value in values)
    if len({len(vector) for vector in result.values()}) != 1:
        raise ShioajiSdkError("shioaji kbars vectors have inconsistent lengths")
    return result


def _kbars_mapping(raw: object) -> Mapping[str, object]:
    """Convert the Shioaji 1.7.1 ``KBars`` struct to its public dict form."""
    if isinstance(raw, Mapping):
        return raw
    to_dict = getattr(raw, "dict", None)
    if not callable(to_dict):
        raise ShioajiSdkError("shioaji kbars result is invalid")
    try:
        mapped = to_dict()
    except Exception:
        raise ShioajiSdkError("shioaji kbars result is invalid") from None
    if not isinstance(mapped, Mapping):
        raise ShioajiSdkError("shioaji kbars result is invalid")
    return mapped


def _normalize_scalar(value: object) -> object:
    """Convert numpy-like scalars without retaining provider-specific objects."""
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = item()
        except Exception:
            return "<invalid_scalar>"
    return (
        value
        if isinstance(value, (int, float, str, bool, Decimal)) or value is None
        else "<invalid_scalar>"
    )


def _usage_bytes(api: object) -> int | None:
    """Read only a non-secret byte metric; never map it to request counts."""
    try:
        usage = getattr(api, "usage")()
        value = usage.get("bytes") if isinstance(usage, Mapping) else getattr(usage, "bytes", None)
        return value if type(value) is int and value >= 0 else None
    except Exception:
        return None
