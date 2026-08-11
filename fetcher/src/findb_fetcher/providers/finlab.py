"""Offline-verifiable mapping for reviewed FinLab daily dataset bundles.

This module deliberately has no FinLab SDK import.  A production runtime may
adapt its SDK behind :class:`FinLabDatasetGateway`; the Fetcher boundary only
accepts a small, deterministic table representation and persists that exact
canonical bundle before it prepares Source delivery.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Protocol

from findb_fetcher.raw_storage import RawPayloadStore, attach_raw_object

FINLAB_SOURCE = "finlab"
_IDENTIFIER = re.compile(r"^[a-z0-9_]{1,50}$")
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,49}$")
_REQUIRED_FIELDS = ("open", "high", "low", "close", "volume")
_REQUIRED_DATASETS = {
    "open": "price:開盤價",
    "high": "price:最高價",
    "low": "price:最低價",
    "close": "price:收盤價",
    "volume": "price:成交股數",
}
_MAX_SOURCE_REQUEST_BYTES = 1024 * 1024


class FinLabError(RuntimeError):
    """Base error for the deliberately narrow FinLab Fetcher boundary."""


class FinLabConfigError(FinLabError):
    """Reviewed FinLab mapping configuration is invalid."""


class FinLabPayloadError(FinLabError):
    """A gateway table cannot safely become a market_eod contract."""


class FinLabSdkError(FinLabError):
    """The optional FinLab SDK is unavailable or returned an unsafe table."""


class FinLabTargetNotReadyError(FinLabSdkError, FinLabPayloadError):
    """The provider has not published the requested target trading date yet."""


@dataclass(frozen=True, slots=True)
class FinLabDatasetTable:
    """One provider dataset expressed without pandas or an SDK dependency."""

    dates: tuple[str, ...]
    symbols: tuple[str, ...]
    values: tuple[tuple[object, ...], ...]


class FinLabDatasetGateway(Protocol):
    """Injectable production seam; implementations must not leak credentials."""

    def fetch_dataset(
        self,
        dataset: str,
        *,
        target_date: date,
        symbols: Sequence[str],
    ) -> FinLabDatasetTable: ...


@dataclass(frozen=True, slots=True)
class FinLabSdkGateway:
    """Headless, lazy FinLab SDK 1.5.7 gateway.

    Importing this module never imports FinLab.  The SDK is loaded only when a
    disabled-by-default caller explicitly invokes ``fetch_dataset``.  Tokens
    are accepted solely through this object or ``FINLAB_API_TOKEN`` and are
    deliberately excluded from representations and error messages.  The
    optional dependency is intentionally pinned to the last release before
    FinLab 1.5.8 introduced its browser-based Firebase login flow.
    """

    api_token: str = dataclass_field(repr=False, compare=False)
    _sdk: object | None = None

    def __post_init__(self) -> None:
        token = self.api_token.strip()
        if not token:
            raise FinLabConfigError("FINLAB_API_TOKEN is required for FinLab SDK access")
        object.__setattr__(self, "api_token", token)

    @classmethod
    def from_env(cls) -> "FinLabSdkGateway":
        return cls(api_token=os.getenv("FINLAB_API_TOKEN", ""))

    def __repr__(self) -> str:
        return "FinLabSdkGateway(api_token=<redacted>)"

    def fetch_dataset(
        self,
        dataset: str,
        *,
        target_date: date,
        symbols: Sequence[str],
    ) -> FinLabDatasetTable:
        if not isinstance(dataset, str) or not dataset.strip():
            raise FinLabConfigError("FinLab dataset name must be a non-empty string")
        if not isinstance(target_date, date) or isinstance(target_date, datetime):
            raise FinLabConfigError("FinLab target_date must be a date")
        reviewed_symbols = tuple(symbols)
        if (
            not reviewed_symbols
            or len(set(reviewed_symbols)) != len(reviewed_symbols)
            or any(
                not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None
                for symbol in reviewed_symbols
            )
        ):
            raise FinLabConfigError("FinLab reviewed symbols are invalid")
        sdk = self._sdk
        if sdk is None:
            try:
                sdk = importlib.import_module("finlab")
            except ImportError as exc:
                raise FinLabSdkError("FinLab SDK is not installed") from exc
        login = getattr(sdk, "login", None)
        data = getattr(sdk, "data", None)
        if data is None:
            try:
                data = importlib.import_module("finlab.data")
            except ImportError:
                data = None
        get = getattr(data, "get", None)
        if not callable(login) or not callable(get):
            raise FinLabSdkError("FinLab SDK does not expose the required headless data API")
        try:
            login(self.api_token)
            frame = get(dataset.strip())
        except Exception:
            # Never retain provider exceptions: some SDK errors include request context.
            raise FinLabSdkError("FinLab SDK dataset retrieval failed") from None
        try:
            return _table_from_dataframe(
                frame,
                target_date=target_date,
                reviewed_symbols=reviewed_symbols,
            )
        except FinLabSdkError as exc:
            # Preserve the safe publication-miss subtype while discarding any
            # provider traceback or credential-bearing context.
            raise type(exc)(str(exc)) from None
        except Exception:
            raise FinLabSdkError("FinLab SDK dataset result failed validation") from None


@dataclass(frozen=True, slots=True)
class FinLabSymbol:
    """A reviewed source-to-canonical symbol mapping; never inferred at runtime."""

    source_symbol: str
    canonical_symbol: str

    @property
    def symbol(self) -> str:
        """Generic-universe spelling for the provider symbol."""

        return self.source_symbol


@dataclass(frozen=True, slots=True)
class FinLabDatasetConfig:
    """Explicit reviewed scope for one asset class and one daily OHLCV feed."""

    dataset_key: str
    market: str
    asset_class: str
    currency: str | None
    field_datasets: Mapping[str, str]
    symbols: tuple[FinLabSymbol, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_datasets", MappingProxyType(dict(self.field_datasets)))
        object.__setattr__(self, "symbols", tuple(self.symbols))
        if _IDENTIFIER.fullmatch(self.dataset_key) is None:
            raise FinLabConfigError("dataset_key must be a lowercase identifier")
        if self.market != "TW":
            raise FinLabConfigError("FinLab v1 mapping supports only market TW")
        if self.asset_class != "equity":
            raise FinLabConfigError("FinLab staging feed supports only asset_class equity")
        if self.dataset_key != "tw_equity_eod":
            raise FinLabConfigError("asset_class must match its fixed FinLab Source dataset key")
        if self.currency not in {None, "TWD"}:
            raise FinLabConfigError("currency must be TWD or null")
        if dict(self.field_datasets) != _REQUIRED_DATASETS:
            raise FinLabConfigError("field_datasets must match the reviewed FinLab OHLCV datasets")
        if not self.symbols:
            raise FinLabConfigError("reviewed FinLab symbol scope must not be empty")
        source_symbols: set[str] = set()
        canonical_symbols: set[str] = set()
        for symbol in self.symbols:
            if _SYMBOL.fullmatch(symbol.source_symbol) is None:
                raise FinLabConfigError("reviewed source symbol is invalid")
            if _SYMBOL.fullmatch(symbol.canonical_symbol) is None:
                raise FinLabConfigError("reviewed canonical symbol is invalid")
            if (
                symbol.source_symbol in source_symbols
                or symbol.canonical_symbol in canonical_symbols
            ):
                raise FinLabConfigError("reviewed symbol mapping contains duplicates")
            source_symbols.add(symbol.source_symbol)
            canonical_symbols.add(symbol.canonical_symbol)


@dataclass(frozen=True, slots=True)
class FinLabDatasetBundle:
    """Canonical, content-addressed representation of a provider dataset snapshot."""

    config: FinLabDatasetConfig
    target_date: date
    field_values: Mapping[str, Mapping[str, str]]
    raw_bytes: bytes
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.config, FinLabDatasetConfig):
            raise FinLabPayloadError("bundle config is invalid")
        if not isinstance(self.target_date, date) or isinstance(self.target_date, datetime):
            raise FinLabPayloadError("bundle target date is invalid")
        if not isinstance(self.raw_bytes, bytes):
            raise FinLabPayloadError("bundle raw bytes are invalid")
        frozen_fields = MappingProxyType(
            {field: MappingProxyType(dict(values)) for field, values in self.field_values.items()}
        )
        object.__setattr__(self, "field_values", frozen_fields)
        expected_bytes = _bundle_bytes(
            config=self.config,
            target_date=self.target_date,
            field_values=frozen_fields,
        )
        expected_sha256 = hashlib.sha256(expected_bytes).hexdigest()
        if self.raw_bytes != expected_bytes or self.sha256 != expected_sha256:
            raise FinLabPayloadError("bundle bytes, checksum, config, and fields must agree")


def fetch_finlab_dataset_bundle(
    gateway: FinLabDatasetGateway,
    *,
    config: FinLabDatasetConfig,
    target_date: date,
) -> FinLabDatasetBundle:
    """Fetch the configured datasets through an injected gateway and canonicalize them."""
    reviewed_symbols = tuple(symbol.source_symbol for symbol in config.symbols)
    tables = {
        field: gateway.fetch_dataset(
            dataset,
            target_date=target_date,
            symbols=reviewed_symbols,
        )
        for field, dataset in config.field_datasets.items()
    }
    return build_finlab_dataset_bundle(config=config, target_date=target_date, tables=tables)


def build_finlab_dataset_bundle(
    *,
    config: FinLabDatasetConfig,
    target_date: date,
    tables: Mapping[str, FinLabDatasetTable],
) -> FinLabDatasetBundle:
    """Validate table grids and serialize a deterministic ``finlab_dataset_bundle.v1``."""
    if set(tables) != set(_REQUIRED_FIELDS):
        raise FinLabPayloadError("FinLab tables must contain exactly OHLCV")
    expected_symbols = tuple(symbol.source_symbol for symbol in config.symbols)
    normalized: dict[str, dict[str, str]] = {}
    expected_grid: tuple[tuple[str, ...], tuple[str, ...]] | None = None
    for field in _REQUIRED_FIELDS:
        table = tables[field]
        dates, symbols, values = _validate_table(table, field=field, target_date=target_date)
        grid = (dates, symbols)
        if expected_grid is None:
            expected_grid = grid
        elif grid != expected_grid:
            raise FinLabPayloadError("FinLab logical field grids do not match")
        if set(symbols) != set(expected_symbols):
            raise FinLabPayloadError("FinLab table symbols do not match reviewed scope")
        row = values[0]
        normalized[field] = {
            source_symbol: _numeric_text(row[symbols.index(source_symbol)], field=field)
            for source_symbol in sorted(expected_symbols)
        }

    _validate_ohlcv(normalized, expected_symbols)
    raw_bytes = _bundle_bytes(config=config, target_date=target_date, field_values=normalized)
    return FinLabDatasetBundle(
        config=config,
        target_date=target_date,
        field_values=normalized,
        raw_bytes=raw_bytes,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def build_market_eod_request(
    bundle: FinLabDatasetBundle,
    *,
    fetched_at: datetime,
    delivery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Map only the reviewed bundle scope to a provider-neutral ingress request."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise FinLabPayloadError("fetched_at must be timezone-aware")
    rows: list[dict[str, Any]] = []
    for symbol in sorted(bundle.config.symbols, key=lambda item: item.canonical_symbol):
        row: dict[str, Any] = {
            "symbol": symbol.canonical_symbol,
            "source_symbol": symbol.source_symbol,
            "trade_date": bundle.target_date.isoformat(),
            "currency": bundle.config.currency,
            "open": bundle.field_values["open"][symbol.source_symbol],
            "high": bundle.field_values["high"][symbol.source_symbol],
            "low": bundle.field_values["low"][symbol.source_symbol],
            "close": bundle.field_values["close"][symbol.source_symbol],
            "volume": _integer_value(bundle.field_values["volume"][symbol.source_symbol]),
        }
        rows.append(row)
    identity = hashlib.sha256(bundle.raw_bytes).hexdigest()
    request: dict[str, Any] = {
        "dataset_key": bundle.config.dataset_key,
        "schema_id": "market_eod",
        "schema_version": 1,
        "source": FINLAB_SOURCE,
        "request_key": f"finlab:{bundle.config.dataset_key}:{identity[:32]}",
        "idempotency_key": f"finlab:{identity}",
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": {
            "batch": {
                "data_date": bundle.target_date.isoformat(),
                # The reviewed FinLab bundle is a complete snapshot of its
                # governed two-symbol universe.  It must never be interpreted
                # as a partial/incremental update by Source normalization.
                "delivery_mode": "full_snapshot",
                "declared_record_count": len(rows),
            },
            "data": rows,
        },
    }
    if delivery is not None:
        request["delivery"] = dict(delivery)
    encoded = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) >= _MAX_SOURCE_REQUEST_BYTES:
        raise FinLabPayloadError("FinLab Source request must be smaller than 1 MiB")
    return request


def prepare_market_eod_delivery(
    bundle: FinLabDatasetBundle,
    *,
    fetched_at: datetime,
    raw_store: RawPayloadStore,
    delivery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist raw bytes before preparing a request; the returned state has raw provenance."""
    raw_object = raw_store.persist(
        bundle.raw_bytes,
        dataset_key=bundle.config.dataset_key,
        source_symbol="dataset_bundle",
        provider=FINLAB_SOURCE,
    )
    if raw_object.sha256 != bundle.sha256 or raw_object.size_bytes != len(bundle.raw_bytes):
        raise FinLabPayloadError("persisted FinLab raw object does not match the bundle")
    request = build_market_eod_request(bundle, fetched_at=fetched_at, delivery=delivery)
    attach_raw_object(request, raw_object)
    return request


def _validate_table(
    table: FinLabDatasetTable,
    *,
    field: str,
    target_date: date,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[object, ...], ...]]:
    if not isinstance(table, FinLabDatasetTable):
        raise FinLabPayloadError(f"{field} table has an unsupported representation")
    if not table.dates or len(table.dates) != len(table.values):
        raise FinLabPayloadError(f"{field} table date grid is invalid")
    if len(set(table.dates)) != len(table.dates):
        raise FinLabPayloadError(f"{field} table has duplicate dates")
    if target_date.isoformat() not in table.dates:
        raise FinLabTargetNotReadyError(f"{field} table does not match target date")
    if tuple(table.dates) != (target_date.isoformat(),):
        raise FinLabPayloadError(f"{field} table does not match target date")
    if not table.symbols or len(set(table.symbols)) != len(table.symbols):
        raise FinLabPayloadError(f"{field} table symbol grid is invalid")
    if any(_SYMBOL.fullmatch(symbol) is None for symbol in table.symbols):
        raise FinLabPayloadError(f"{field} table contains an invalid symbol")
    if any(len(row) != len(table.symbols) for row in table.values):
        raise FinLabPayloadError(f"{field} table value grid is inconsistent")
    return table.dates, table.symbols, table.values


def _table_from_dataframe(
    frame: object,
    *,
    target_date: date,
    reviewed_symbols: tuple[str, ...],
) -> FinLabDatasetTable:
    """Narrow a pandas-like FinLab frame to exactly one validated date row."""
    index = getattr(frame, "index", None)
    columns = getattr(frame, "columns", None)
    loc = getattr(frame, "loc", None)
    if index is None or columns is None or loc is None:
        raise FinLabSdkError("FinLab dataset result is not a DataFrame")
    try:
        index_values = tuple(index)
        column_values = tuple(columns)
    except TypeError as exc:
        raise FinLabSdkError("FinLab dataset axes are not iterable") from exc
    dates = tuple(_frame_index_date(value) for value in index_values)
    if len(set(dates)) != len(dates):
        raise FinLabSdkError("FinLab dataset has duplicate date index values")
    if any(not isinstance(value, str) for value in column_values):
        raise FinLabSdkError("FinLab dataset has non-string symbol columns")
    if len(set(column_values)) != len(column_values):
        raise FinLabSdkError("FinLab dataset has duplicate symbol columns")
    missing_symbols = set(reviewed_symbols).difference(column_values)
    if missing_symbols:
        raise FinLabSdkError("FinLab dataset is missing reviewed symbols")
    matching = [position for position, value in enumerate(dates) if value == target_date]
    if len(matching) != 1:
        raise FinLabSdkError("FinLab dataset does not contain exactly one target date")
    try:
        row = tuple(loc[index_values[matching[0]], symbol] for symbol in reviewed_symbols)
    except Exception as exc:
        raise FinLabSdkError("FinLab dataset target row cannot be read") from exc
    return FinLabDatasetTable(
        dates=(target_date.isoformat(),),
        symbols=reviewed_symbols,
        values=(row,),
    )


def _frame_index_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    as_date = getattr(value, "date", None)
    if callable(as_date):
        candidate = as_date()
        if isinstance(candidate, date):
            return candidate
    raise FinLabSdkError("FinLab dataset index must contain date values")


def _bundle_bytes(
    *,
    config: FinLabDatasetConfig,
    target_date: date,
    field_values: Mapping[str, Mapping[str, str]],
) -> bytes:
    payload = {
        "bundle_schema": "finlab_dataset_bundle.v1",
        "dataset_key": config.dataset_key,
        "market": config.market,
        "asset_class": config.asset_class,
        "currency": config.currency,
        "target_date": target_date.isoformat(),
        "field_datasets": {
            field: config.field_datasets[field] for field in sorted(_REQUIRED_FIELDS)
        },
        "fields": {field: dict(field_values[field]) for field in sorted(_REQUIRED_FIELDS)},
        "symbols": [
            {"source_symbol": item.source_symbol, "canonical_symbol": item.canonical_symbol}
            for item in sorted(config.symbols, key=lambda item: item.source_symbol)
        ],
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _numeric_text(value: object, *, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise FinLabPayloadError(f"{field} contains a non-numeric value")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FinLabPayloadError(f"{field} contains a non-numeric value") from exc
    if not decimal.is_finite() or decimal < 0:
        raise FinLabPayloadError(f"{field} must be finite and non-negative")
    return format(decimal, "f")


def _integer_value(value: str) -> int:
    decimal = Decimal(value)
    if decimal != decimal.to_integral_value():
        raise FinLabPayloadError("volume must be an integer")
    return int(decimal)


def _validate_ohlcv(values: Mapping[str, Mapping[str, str]], symbols: Sequence[str]) -> None:
    for symbol in symbols:
        open_value = Decimal(values["open"][symbol])
        high_value = Decimal(values["high"][symbol])
        low_value = Decimal(values["low"][symbol])
        close_value = Decimal(values["close"][symbol])
        if high_value < max(open_value, low_value, close_value):
            raise FinLabPayloadError(f"{symbol} high violates OHLC bounds")
        if low_value > min(open_value, high_value, close_value):
            raise FinLabPayloadError(f"{symbol} low violates OHLC bounds")
        _integer_value(values["volume"][symbol])
