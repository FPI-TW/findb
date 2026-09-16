"""Strict, versioned symbol-universe configuration for bounded fetch runs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

_MAX_CONFIG_BYTES = 64 * 1024
_MAX_SYMBOLS_ABSOLUTE_V1 = 5
_MAX_SYMBOLS_ABSOLUTE_V2 = 128
_MAX_RECORDS_PER_SYMBOL_ABSOLUTE = 5000
_MAX_TOTAL_RECORDS_ABSOLUTE = 10_000
_MAX_DATE_SPAN_DAYS_ABSOLUTE = 3660
_MAX_CREDITS_ABSOLUTE = 5
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,49}$")
_EXCHANGE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9 ._&/+:-]{0,99}$")
_ROOT_KEYS = {
    "universe_version",
    "universe_id",
    "provider",
    "dataset_key",
    "market",
    "asset_class",
    "instrument_type",
    "credit_cost_per_symbol",
    "limits",
    "symbols",
}
_V2_GOVERNANCE_KEYS = {
    "source_url",
    "effective_date",
    "source_sha256",
    "symbols_sha256",
    "batch_size",
    "inter_batch_seconds",
}
_LIMIT_KEYS = {
    "max_symbols_per_run",
    "max_records_per_symbol",
    "max_total_records_per_run",
    "max_date_span_days",
    "max_credits_per_run",
}
_SYMBOL_KEYS = {"symbol", "canonical_symbol", "exchange"}


class UniverseError(ValueError):
    """A governed symbol universe or requested run violates its safety contract."""


class _DuplicateJSONKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UniverseLimits:
    max_symbols_per_run: int
    max_records_per_symbol: int
    max_total_records_per_run: int
    max_date_span_days: int
    max_credits_per_run: int


@dataclass(frozen=True, slots=True)
class UniverseSymbol:
    symbol: str
    canonical_symbol: str
    exchange: str


@dataclass(frozen=True, slots=True)
class SymbolUniverse:
    universe_version: int
    universe_id: str
    provider: str
    dataset_key: str
    market: str
    asset_class: str
    instrument_type: str
    credit_cost_per_symbol: int
    limits: UniverseLimits
    symbols: tuple[UniverseSymbol, ...]
    source_url: str | None = None
    effective_date: date | None = None
    source_sha256: str | None = None
    symbols_sha256: str | None = None
    batch_size: int | None = None
    inter_batch_seconds: int | None = None

    @property
    def estimated_credits(self) -> int:
        members = len(self.symbols)
        if self.universe_version == 2:
            members = min(members, self.limits.max_symbols_per_run)
        return members * self.credit_cost_per_symbol

    def validate_query_bounds(
        self,
        *,
        start_date: date | None,
        end_date: date | None,
        outputsize: int | None,
    ) -> None:
        if (start_date is None) != (end_date is None):
            raise UniverseError("universe runs require both start_date and end_date")
        if start_date is not None and outputsize is not None:
            raise UniverseError("universe runs cannot combine a date window with outputsize")
        if start_date is None and outputsize is None:
            raise UniverseError("universe runs require a date window or outputsize")
        if start_date is not None and end_date is not None:
            if start_date > end_date:
                raise UniverseError("start_date must not be after end_date")
            if (end_date - start_date).days > self.limits.max_date_span_days:
                raise UniverseError("requested date span exceeds universe limit")
        if outputsize is not None and (
            outputsize < 1 or outputsize > self.limits.max_records_per_symbol
        ):
            raise UniverseError("outputsize exceeds universe per-symbol record limit")


def load_symbol_universe(path: Path) -> SymbolUniverse:
    """Load a strict governed universe without accepting unknown fields."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UniverseError(f"unable to read universe config: {path}") from exc
    if len(raw) > _MAX_CONFIG_BYTES:
        raise UniverseError("universe config exceeds size limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKeyError) as exc:
        raise UniverseError("universe config must be valid UTF-8 JSON with unique keys") from exc
    if not isinstance(value, dict):
        raise UniverseError("universe config must be an object")

    universe_version = _positive_int(value, "universe_version")
    expected_keys = _ROOT_KEYS if universe_version == 1 else _ROOT_KEYS | _V2_GOVERNANCE_KEYS
    if universe_version not in {1, 2} or set(value) != expected_keys:
        raise UniverseError(
            f"universe v{universe_version} keys must be exactly {sorted(expected_keys)}"
        )
    universe_id = _identifier(value, "universe_id")
    provider = _identifier(value, "provider")
    dataset_key = _identifier(value, "dataset_key")
    market = _required_string(value, "market")
    asset_class = _required_string(value, "asset_class")
    instrument_type = _required_string(value, "instrument_type")
    credit_cost_per_symbol = _positive_int(value, "credit_cost_per_symbol")

    if provider != "twelve_data":
        raise UniverseError("provider must be twelve_data")
    if dataset_key != "us_equity_eod":
        raise UniverseError("dataset_key must be us_equity_eod")
    if market != "US" or asset_class != "equity" or instrument_type != "Common Stock":
        raise UniverseError("universe supports only US Common Stock")
    if credit_cost_per_symbol != 1:
        raise UniverseError("Twelve Data time_series credit cost must be 1 per symbol")

    absolute_symbols = (
        _MAX_SYMBOLS_ABSOLUTE_V1 if universe_version == 1 else _MAX_SYMBOLS_ABSOLUTE_V2
    )
    limits = _load_limits(value["limits"])
    symbols = _load_symbols(value["symbols"], absolute_symbols=absolute_symbols)
    if universe_version == 1 and len(symbols) > limits.max_symbols_per_run:
        raise UniverseError("symbol count exceeds max_symbols_per_run")
    estimated_credits = min(len(symbols), limits.max_symbols_per_run) * credit_cost_per_symbol
    if estimated_credits > limits.max_credits_per_run:
        raise UniverseError("estimated credits exceed max_credits_per_run")
    if limits.max_total_records_per_run < limits.max_records_per_symbol:
        raise UniverseError("max_total_records_per_run must be at least max_records_per_symbol")

    governance: dict[str, Any] = {}
    if universe_version == 2:
        source_url = _required_string(value, "source_url")
        if not source_url.startswith("https://"):
            raise UniverseError("source_url must use HTTPS")
        effective_date = _iso_date(value, "effective_date")
        source_sha256 = _sha256(value, "source_sha256")
        symbols_sha256 = _sha256(value, "symbols_sha256")
        expected_symbols_sha256 = hashlib.sha256(
            ("\n".join(symbol.canonical_symbol for symbol in symbols) + "\n").encode()
        ).hexdigest()
        if symbols_sha256 != expected_symbols_sha256:
            raise UniverseError("symbols_sha256 does not match normalized canonical symbols")
        batch_size = _positive_int(value, "batch_size")
        inter_batch_seconds = _positive_int(value, "inter_batch_seconds")
        if batch_size != limits.max_symbols_per_run or batch_size > 5:
            raise UniverseError("v2 batch_size must equal max_symbols_per_run and be at most 5")
        if inter_batch_seconds < 60:
            raise UniverseError("v2 inter_batch_seconds must be at least 60")
        governance = {
            "source_url": source_url,
            "effective_date": effective_date,
            "source_sha256": source_sha256,
            "symbols_sha256": symbols_sha256,
            "batch_size": batch_size,
            "inter_batch_seconds": inter_batch_seconds,
        }
    return SymbolUniverse(
        universe_version=universe_version,
        universe_id=universe_id,
        provider=provider,
        dataset_key=dataset_key,
        market=market,
        asset_class=asset_class,
        instrument_type=instrument_type,
        credit_cost_per_symbol=credit_cost_per_symbol,
        limits=limits,
        symbols=symbols,
        **governance,
    )


def _load_limits(value: Any) -> UniverseLimits:
    if not isinstance(value, dict) or set(value) != _LIMIT_KEYS:
        raise UniverseError(f"limits keys must be exactly {sorted(_LIMIT_KEYS)}")
    limits = UniverseLimits(
        max_symbols_per_run=_positive_int(value, "max_symbols_per_run"),
        max_records_per_symbol=_positive_int(value, "max_records_per_symbol"),
        max_total_records_per_run=_positive_int(value, "max_total_records_per_run"),
        max_date_span_days=_positive_int(value, "max_date_span_days"),
        max_credits_per_run=_positive_int(value, "max_credits_per_run"),
    )
    absolute_limits = (
        (
            "max_symbols_per_run",
            limits.max_symbols_per_run,
            5,
        ),
        (
            "max_records_per_symbol",
            limits.max_records_per_symbol,
            _MAX_RECORDS_PER_SYMBOL_ABSOLUTE,
        ),
        (
            "max_total_records_per_run",
            limits.max_total_records_per_run,
            _MAX_TOTAL_RECORDS_ABSOLUTE,
        ),
        (
            "max_date_span_days",
            limits.max_date_span_days,
            _MAX_DATE_SPAN_DAYS_ABSOLUTE,
        ),
        (
            "max_credits_per_run",
            limits.max_credits_per_run,
            _MAX_CREDITS_ABSOLUTE,
        ),
    )
    for field, configured, absolute in absolute_limits:
        if configured > absolute:
            raise UniverseError(f"{field} exceeds absolute safety limit {absolute}")
    return limits


def _load_symbols(value: Any, *, absolute_symbols: int) -> tuple[UniverseSymbol, ...]:
    if not isinstance(value, list) or not value:
        raise UniverseError("symbols must be a non-empty list")
    symbols: list[UniverseSymbol] = []
    seen_source: set[str] = set()
    seen_canonical: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != _SYMBOL_KEYS:
            raise UniverseError(f"symbols[{index}] keys must be exactly {sorted(_SYMBOL_KEYS)}")
        source_symbol = _required_string(item, "symbol")
        canonical_symbol = _required_string(item, "canonical_symbol")
        exchange = _required_string(item, "exchange")
        if _SYMBOL_PATTERN.fullmatch(source_symbol) is None:
            raise UniverseError(f"symbols[{index}].symbol is invalid")
        if _SYMBOL_PATTERN.fullmatch(canonical_symbol) is None:
            raise UniverseError(f"symbols[{index}].canonical_symbol is invalid")
        if _EXCHANGE_PATTERN.fullmatch(exchange) is None:
            raise UniverseError(f"symbols[{index}].exchange is invalid")
        if source_symbol in seen_source:
            raise UniverseError(f"duplicate source symbol: {source_symbol}")
        if canonical_symbol in seen_canonical:
            raise UniverseError(f"duplicate canonical symbol: {canonical_symbol}")
        seen_source.add(source_symbol)
        seen_canonical.add(canonical_symbol)
        symbols.append(
            UniverseSymbol(
                symbol=source_symbol,
                canonical_symbol=canonical_symbol,
                exchange=exchange,
            )
        )
    if len(symbols) > absolute_symbols:
        raise UniverseError("symbol count exceeds absolute safety limit")
    return tuple(symbols)


def _iso_date(parent: dict[str, Any], key: str) -> date:
    value = _required_string(parent, key)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise UniverseError(f"{key} must be an ISO date") from exc


def _sha256(parent: dict[str, Any], key: str) -> str:
    value = _required_string(parent, key)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise UniverseError(f"{key} must be a lowercase SHA-256 digest")
    return value


def _required_string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise UniverseError(f"{key} must be a non-empty string")
    return value.strip()


def _identifier(parent: dict[str, Any], key: str) -> str:
    value = _required_string(parent, key)
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise UniverseError(f"{key} must be a stable lowercase identifier")
    return value


def _positive_int(parent: dict[str, Any], key: str) -> int:
    value = parent.get(key)
    if type(value) is not int or value < 1:
        raise UniverseError(f"{key} must be a positive integer")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(key)
        result[key] = value
    return result
