"""Narrow, lazy Shioaji 1.7.1 boundary and Taiwan minute-bar adapter.

The SDK is intentionally imported only by ``ShioajiSdkGateway.fetch_kbars``.
No provider objects or credential-bearing exceptions cross this boundary.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import multiprocessing
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Protocol
from zoneinfo import ZoneInfo

SHIOAJI_SOURCE = "shioaji"
TAIPEI = ZoneInfo("Asia/Taipei")
MAX_SYMBOLS = 50
MAX_ROWS = 15_000
_SYMBOL_MAX = 50
MAX_ISOLATED_IPC_BYTES = 1024 * 1024 + 1024
_ISOLATED_ERROR_STAGES = {
    "CREDENTIALS": "credentials",
    "LOGIN": "login",
    "SDK": "sdk",
    "CONTRACT": "contract",
    "PAYLOAD": "payload",
    "ACQUISITION": "acquisition",
}
PAYLOAD_REASONS = frozenset(
    {
        "kbars_shape",
        "kbars_scalar",
        "child_boundary",
        "ipc_encode",
        "ipc_size",
        "ipc_transport",
        "ipc_schema",
        "snapshot",
        "contract_access",
        "contract_mapping",
    }
)
_SIMULATION_TRUE = frozenset({"1", "true", "yes", "on"})
_SIMULATION_FALSE = frozenset({"0", "false", "no", "off"})
# These identities are the only catalog-free contracts reviewed against the
# Shioaji 1.7.1 simulation API.  Do not infer an exchange for other symbols.
_REVIEWED_BASE_CONTRACTS: Mapping[str, tuple[str, str, str]] = MappingProxyType(
    {
        "2330": ("STK", "TSE", "TW"),
        "0050": ("STK", "TSE", "TW"),
        "0056": ("STK", "TSE", "TW"),
        "006201": ("STK", "TSE", "TW"),
    }
)


class ShioajiError(RuntimeError):
    """Base class with deliberately credential-free messages."""


class ShioajiConfigError(ShioajiError):
    pass


class ShioajiSdkError(ShioajiError):
    def __init__(
        self, message: str, *, code: str = "ACQUISITION", reason: str | None = None
    ) -> None:
        self.code = code
        self.reason = (
            reason
            if code == "PAYLOAD" and isinstance(reason, str) and reason in PAYLOAD_REASONS
            else None
        )
        super().__init__(message)


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
class IsolatedShioajiGateway:
    """Run the credential-bearing SDK in a short-lived, silent child process.

    IPC accepts only a small, SDK-detached Kbars representation and a coarse
    code; neither provider diagnostics nor credentials can cross the boundary.
    """

    api_key: str = field(repr=False, compare=False)
    secret_key: str = field(repr=False, compare=False)
    timeout_seconds: float = 30.0
    simulation: bool = True

    @classmethod
    def from_env(cls) -> "IsolatedShioajiGateway":
        return cls(
            os.getenv("SHIOAJI_API_KEY", ""),
            os.getenv("SHIOAJI_SECRET_KEY", ""),
            simulation=_simulation_from_env(),
        )

    def fetch_kbars(self, symbol: str, target_date: date) -> ShioajiKbarsSnapshot:
        _validate_symbol(symbol)
        parent, child = multiprocessing.Pipe(duplex=False)
        proc = multiprocessing.Process(
            target=_isolated_fetch_child,
            args=(
                child,
                self.api_key,
                self.secret_key,
                self.simulation,
                symbol,
                target_date.isoformat(),
            ),
            daemon=True,
        )
        proc.start()
        child.close()
        try:
            if not parent.poll(self.timeout_seconds):
                proc.terminate()
                proc.join(2)
                if proc.is_alive():
                    proc.kill()
                    proc.join(2)
                raise ShioajiSdkError("shioaji acquisition timed out")
            raw = parent.recv_bytes(MAX_ISOLATED_IPC_BYTES)
        except (EOFError, OSError, ValueError):
            # A child that cannot produce one bounded reviewed message is an
            # invalid IPC payload, not a retryable provider acquisition.
            raise ShioajiSdkError(
                "shioaji acquisition failed", code="PAYLOAD", reason="ipc_transport"
            ) from None
        finally:
            parent.close()
            if proc.is_alive():
                proc.terminate()
                proc.join(2)
            if proc.is_alive():
                proc.kill()
                proc.join(2)
            else:
                proc.join()
        message = _decode_isolated_message(raw)
        if message["code"] != "OK":
            raise ShioajiSdkError(
                "shioaji acquisition failed",
                code=str(message["code"]),
                reason=message.get("reason"),
            )
        kbars = message["kbars"]
        return ShioajiKbarsSnapshot(
            {key: tuple(value) for key, value in kbars.items()},
            message["before"],
            message["after"],
        )


def _decode_isolated_message(raw: bytes) -> dict[str, Any]:
    """Decode only bounded JSON with a reviewed, diagnostic-free schema."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_ISOLATED_IPC_BYTES:
        raise ShioajiSdkError("shioaji acquisition failed", code="PAYLOAD", reason="ipc_schema")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise ShioajiSdkError(
            "shioaji acquisition failed", code="PAYLOAD", reason="ipc_schema"
        ) from None
    if not isinstance(value, dict):
        raise ShioajiSdkError("shioaji acquisition failed", code="PAYLOAD", reason="ipc_schema")
    code = value.get("code")
    if isinstance(code, str) and code in _ISOLATED_ERROR_STAGES:
        allowed = {"code", "stage"} if code != "PAYLOAD" else {"code", "stage", "reason"}
        if set(value) == allowed and value.get("stage") == _ISOLATED_ERROR_STAGES[code]:
            reason = value.get("reason")
            if code != "PAYLOAD" or (isinstance(reason, str) and reason in PAYLOAD_REASONS):
                return {"code": code} if code != "PAYLOAD" else {"code": code, "reason": reason}
        raise ShioajiSdkError("shioaji acquisition failed", code="PAYLOAD", reason="ipc_schema")
    if set(value) != {"code", "stage", "kbars", "before", "after"} or code != "OK":
        raise ShioajiSdkError("shioaji acquisition failed", code="PAYLOAD", reason="ipc_schema")
    if value["stage"] != "payload" or not isinstance(value["kbars"], dict):
        raise ShioajiSdkError("shioaji acquisition failed", code="PAYLOAD", reason="ipc_schema")
    kbars = value["kbars"]
    if (
        not kbars
        or any(
            not isinstance(key, str) or not isinstance(items, list) for key, items in kbars.items()
        )
        or any(
            type(counter) is not int and counter is not None
            for counter in (value["before"], value["after"])
        )
        or any(
            type(counter) is int and counter < 0 for counter in (value["before"], value["after"])
        )
    ):
        raise ShioajiSdkError("shioaji acquisition failed", code="PAYLOAD", reason="ipc_schema")
    try:
        normalized = _plain_kbars(kbars)
    except ShioajiSdkError:
        raise ShioajiSdkError(
            "shioaji acquisition failed", code="PAYLOAD", reason="ipc_schema"
        ) from None
    return {
        "code": "OK",
        "kbars": {key: list(items) for key, items in normalized.items()},
        "before": value["before"],
        "after": value["after"],
    }


def _isolated_error_message(code: object, reason: object = None) -> dict[str, str]:
    """Map only reviewed child codes; unknown taxonomy is terminal payload."""
    reviewed = code if isinstance(code, str) and code in _ISOLATED_ERROR_STAGES else "PAYLOAD"
    message = {"code": reviewed, "stage": _ISOLATED_ERROR_STAGES[reviewed]}
    if reviewed == "PAYLOAD":
        message["reason"] = (
            reason if isinstance(reason, str) and reason in PAYLOAD_REASONS else "child_boundary"
        )
    return message


_STATIC_ERROR_WIRES = {
    (code, reason): json.dumps(
        _isolated_error_message(code, reason), separators=(",", ":"), allow_nan=False
    ).encode()
    for code, reason in (
        ("CREDENTIALS", None),
        ("LOGIN", None),
        ("SDK", None),
        ("CONTRACT", None),
        ("ACQUISITION", None),
        *(("PAYLOAD", reason) for reason in PAYLOAD_REASONS),
    )
}


def _isolated_error_wire(code: object, reason: object = None) -> bytes:
    reviewed = code if isinstance(code, str) and code in _ISOLATED_ERROR_STAGES else "PAYLOAD"
    normalized_reason = (
        reason if isinstance(reason, str) and reason in PAYLOAD_REASONS else "child_boundary"
    )
    return (
        _STATIC_ERROR_WIRES[(reviewed, None)]
        if reviewed != "PAYLOAD"
        else _STATIC_ERROR_WIRES[("PAYLOAD", normalized_reason)]
    )


def _encode_isolated_success(message: dict[str, Any]) -> bytes:
    """Encode the complete reviewed success envelope exactly once."""
    return json.dumps(message, separators=(",", ":"), allow_nan=False).encode()


def _bounded_isolated_success_wire(message: dict[str, Any]) -> bytes:
    """Return the exact success bytes or one static, bounded failure wire."""
    try:
        wire = _encode_isolated_success(message)
    except Exception:
        return _isolated_error_wire("PAYLOAD", "ipc_encode")
    return (
        wire if len(wire) <= MAX_ISOLATED_IPC_BYTES else _isolated_error_wire("PAYLOAD", "ipc_size")
    )


def _isolated_fetch_child(
    conn: object, api_key: str, secret_key: str, simulation: bool, symbol: str, target: str
) -> None:
    # Python streams and native FD 1/2 are redirected before SDK import.
    wire: bytes
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 1)
        os.dup2(null, 2)
        os.close(null)
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
        result = ShioajiSdkGateway(api_key, secret_key, simulation=simulation).fetch_kbars(
            symbol, date.fromisoformat(target)
        )
        message: dict[str, Any] = {
            "code": "OK",
            "stage": "payload",
            "kbars": {k: list(v) for k, v in result.kbars.items()},
            "before": result.usage_bytes_before,
            "after": result.usage_bytes_after,
        }
        wire = _bounded_isolated_success_wire(message)
    except ShioajiConfigError:
        wire = _isolated_error_wire("CREDENTIALS")
    except ShioajiSdkError as exc:
        wire = _isolated_error_wire(exc.code, exc.reason)
    except Exception:
        wire = _isolated_error_wire("PAYLOAD", "child_boundary")
    try:
        conn.send_bytes(wire)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        conn.close()  # type: ignore[attr-defined]
    except Exception:
        pass


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
                raise ShioajiSdkError("shioaji SDK version is unsupported", code="SDK")
            sdk = self._sdk if self._sdk is not None else importlib.import_module("shioaji")
            factory = getattr(sdk, "Shioaji")
            api = factory(simulation=self.simulation)
        except ShioajiSdkError:
            raise
        except Exception:
            raise ShioajiSdkError("shioaji SDK is unavailable", code="SDK") from None
        try:
            try:
                api.login(api_key=self.api_key, secret_key=self.secret_key, subscribe_trade=False)
            except Exception:
                raise ShioajiSdkError("shioaji login failed", code="LOGIN") from None
            usage_before = _usage_bytes(api)
            contract = _reviewed_base_contract(sdk, symbol)
            if contract is None:
                contract = _stock_contract(api, symbol)
            try:
                raw = api.kbars(
                    contract,
                    start=target_date.isoformat(),
                    end=target_date.isoformat(),
                )
            except Exception:
                raise ShioajiSdkError(
                    "shioaji kbars retrieval failed",
                    code="ACQUISITION",
                ) from None
            usage_after = _usage_bytes(api)
            return ShioajiKbarsSnapshot(
                _plain_kbars(_kbars_mapping(raw)),
                usage_before,
                usage_after,
            )
        except ShioajiError:
            raise
        except Exception:
            raise ShioajiSdkError("shioaji payload invalid", code="PAYLOAD") from None
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
    sequence: int = 1,
    sequence_count: int = 1,
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
    if (
        type(sequence) is not int
        or type(sequence_count) is not int
        or sequence < 1
        or sequence_count < 1
        or sequence > 99_999
        or sequence_count > 99_999
        or sequence > sequence_count
    ):
        raise ShioajiPayloadError("sequence is invalid")

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
    # This is the canonical backend ``minute_sequence_key_digest`` domain.
    # In particular, sequence_count is deliberately *not* part of it.
    identity_fields: dict[str, object] = {
        "data_date": target_date.isoformat(),
        "dataset_key": dataset_key,
        "sequence": sequence,
        "snapshot_id": snapshot_id,
    }
    identity = json.dumps(
        identity_fields,
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
        "sequence": sequence,
        "sequence_count": sequence_count,
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
    try:
        if set(raw) != set(required):
            raise ShioajiSdkError(
                "shioaji kbars fields are invalid", code="PAYLOAD", reason="kbars_shape"
            )
        result: dict[str, tuple[object, ...]] = {}
        for name in required:
            vector = raw.get(name)
            if isinstance(vector, (str, bytes)) or not isinstance(vector, Iterable):
                raise ShioajiSdkError(
                    "shioaji kbars vectors are invalid", code="PAYLOAD", reason="kbars_shape"
                )
            values = tuple(vector)  # numpy arrays detach here.
            if len(values) > MAX_ROWS:
                raise ShioajiSdkError(
                    "shioaji kbars exceeds bounded rows", code="PAYLOAD", reason="kbars_shape"
                )
            result[name] = tuple(_normalize_scalar(value, name) for value in values)
        if len({len(vector) for vector in result.values()}) != 1:
            raise ShioajiSdkError(
                "shioaji kbars vectors have inconsistent lengths",
                code="PAYLOAD",
                reason="kbars_shape",
            )
        return result
    except ShioajiSdkError:
        raise
    except Exception:
        raise ShioajiSdkError(
            "shioaji kbars vectors are invalid",
            code="PAYLOAD",
            reason="kbars_shape",
        ) from None


def _kbars_mapping(raw: object) -> Mapping[str, object]:
    """Convert the Shioaji 1.7.1 ``KBars`` struct to its public dict form."""
    if isinstance(raw, Mapping):
        return raw
    try:
        to_dict = getattr(raw, "dict", None)
    except Exception:
        raise ShioajiSdkError(
            "shioaji kbars result is invalid", code="PAYLOAD", reason="kbars_shape"
        ) from None
    if not callable(to_dict):
        raise ShioajiSdkError(
            "shioaji kbars result is invalid", code="PAYLOAD", reason="kbars_shape"
        )
    try:
        mapped = to_dict()
    except Exception:
        raise ShioajiSdkError(
            "shioaji kbars result is invalid", code="PAYLOAD", reason="kbars_shape"
        ) from None
    if not isinstance(mapped, Mapping):
        raise ShioajiSdkError(
            "shioaji kbars result is invalid", code="PAYLOAD", reason="kbars_shape"
        )
    return mapped


def _normalize_scalar(value: object, field: str = "Open") -> object:
    """Convert numpy-like scalars without retaining provider-specific objects."""
    try:
        item = getattr(value, "item", None)
    except Exception:
        raise ShioajiSdkError(
            "shioaji kbars scalar is invalid", code="PAYLOAD", reason="kbars_scalar"
        ) from None
    if callable(item):
        try:
            value = item()
        except Exception:
            raise ShioajiSdkError(
                "shioaji kbars scalar is invalid", code="PAYLOAD", reason="kbars_scalar"
            ) from None
    if field == "ts":
        if type(value) is not int or value < 0:
            raise ShioajiSdkError(
                "shioaji kbars scalar is invalid", code="PAYLOAD", reason="kbars_scalar"
            )
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ShioajiSdkError(
            "shioaji kbars scalar is invalid", code="PAYLOAD", reason="kbars_scalar"
        )
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ShioajiSdkError(
            "shioaji kbars scalar is invalid", code="PAYLOAD", reason="kbars_scalar"
        ) from None
    if not numeric.is_finite():
        raise ShioajiSdkError(
            "shioaji kbars scalar is invalid", code="PAYLOAD", reason="kbars_scalar"
        )
    return _decimal_text(numeric) if isinstance(value, (Decimal, str)) else value


def _stock_contract(api: Any, symbol: str) -> object:
    """Read the reviewed lower-case contract path without SDK diagnostics."""
    try:
        contract = api.contracts.stocks.get(symbol)
    except ShioajiError:
        raise
    except Exception:
        raise ShioajiSdkError(
            "shioaji contract access failed", code="PAYLOAD", reason="contract_access"
        ) from None
    if contract is None:
        raise ShioajiSdkError("shioaji contract unavailable", code="CONTRACT")
    return contract


def _reviewed_base_contract(sdk: object, symbol: str) -> object | None:
    """Build only the four live-proven public BaseContract identities.

    This bypasses Shioaji catalog initialization, which can time out in
    simulation.  Unknown symbols deliberately return ``None`` so their
    established catalog behavior remains unchanged.
    """
    identity = _REVIEWED_BASE_CONTRACTS.get(symbol)
    if identity is None:
        return None
    security_type, exchange, region = identity
    try:
        factory = getattr(sdk, "BaseContract")
        return factory(
            security_type=security_type,
            exchange=exchange,
            code=symbol,
            region=region,
        )
    except Exception:
        raise ShioajiSdkError(
            "shioaji contract access failed", code="PAYLOAD", reason="contract_access"
        ) from None


def _simulation_from_env() -> bool:
    """Read the isolated-flow simulation mode without exposing its raw value."""
    raw = os.getenv("SHIOAJI_SIMULATION")
    if raw is None:
        return True
    normalized = raw.strip().lower()
    if normalized in _SIMULATION_TRUE:
        return True
    if normalized in _SIMULATION_FALSE:
        return False
    raise ShioajiConfigError("SHIOAJI_SIMULATION must be a boolean")


def _usage_bytes(api: object) -> int | None:
    """Read only a non-secret byte metric; never map it to request counts."""
    try:
        usage = getattr(api, "usage")()
        value = usage.get("bytes") if isinstance(usage, Mapping) else getattr(usage, "bytes", None)
        return value if type(value) is int and value >= 0 else None
    except Exception:
        return None
