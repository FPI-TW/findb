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
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Protocol

from findb_fetcher.finlab_credentials import effective_token, installed_token, sdk_credential_scope
from findb_fetcher.full_market_state import QuotaBlockedError
from findb_fetcher.provider_work import durable_response
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
_FRAME_JSON_COLUMN_BATCH = 16


class FinLabError(RuntimeError):
    """Base error for the deliberately narrow FinLab Fetcher boundary."""


class FinLabConfigError(FinLabError):
    """Reviewed FinLab mapping configuration is invalid."""


class FinLabPayloadError(FinLabError):
    """A gateway table cannot safely become a market_eod contract."""


class FinLabSdkError(FinLabError):
    """The optional FinLab SDK is unavailable or returned an unsafe table."""


class FinLabOperationalError(FinLabSdkError):
    """Acquisition or accounting failed; never classify this as a mapping gap."""


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
    _identity_credential: str = dataclass_field(default="", init=False, repr=False, compare=False)
    _sdk: object | None = None
    on_response_bytes: Callable[[int], None] | None = dataclass_field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "_identity_credential", self.api_token)
        token = effective_token(self.api_token)
        if not token:
            raise FinLabConfigError("FINLAB_API_TOKEN is required for FinLab SDK access")
        object.__setattr__(self, "api_token", token)

    @classmethod
    def from_env(
        cls, *, on_response_bytes: Callable[[int], None] | None = None
    ) -> "FinLabSdkGateway":
        return cls(api_token=installed_token(), on_response_bytes=on_response_bytes)

    def __repr__(self) -> str:
        return "FinLabSdkGateway(api_token=<redacted>)"

    def fetch_dataset(
        self,
        dataset: str,
        *,
        target_date: date,
        symbols: Sequence[str],
    ) -> FinLabDatasetTable:
        if not isinstance(target_date, date) or isinstance(target_date, datetime):
            raise FinLabConfigError("FinLab target_date must be a date")
        table = self._fetch(dataset, target_date=target_date, symbols=symbols)
        if table.dates != (target_date.isoformat(),):
            raise FinLabTargetNotReadyError(
                "FinLab dataset does not contain exactly one target date"
            )
        return table

    def fetch_catalogue(self, *, symbols: Sequence[str]) -> FinLabDatasetTable:
        """Observe column presence, without claiming any trading-date data exists."""
        return self._fetch("price:收盤價", target_date=None, symbols=symbols)

    @durable_response(
        "finlab_dataset",
        ("dataset", "target_date", "symbols"),
        target_valid=lambda table, args: (
            args["target_date"] is None or table.dates == (args["target_date"].isoformat(),)
        ),
    )
    def _fetch(
        self, dataset: str, *, target_date: date | None, symbols: Sequence[str]
    ) -> FinLabDatasetTable:
        if not isinstance(dataset, str) or not dataset.strip():
            raise FinLabConfigError("FinLab dataset name must be a non-empty string")
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
                raise FinLabOperationalError("FinLab SDK is not installed") from exc
        login = getattr(sdk, "login", None)
        data = getattr(sdk, "data", None)
        if data is None:
            try:
                data = importlib.import_module("finlab.data")
            except ImportError:
                data = None
        get = getattr(data, "get", None)
        if not callable(login) or not callable(get):
            raise FinLabOperationalError(
                "FinLab SDK does not expose the required headless data API"
            )
        from findb_fetcher.account_governor import provider_permit
        from findb_fetcher.acquisition import Acquisition

        permit = provider_permit("finlab")
        account = bool(permit) or self.on_response_bytes is not None
        try:
            with sdk_credential_scope():
                storages = _file_storages(data, get)
                _disable_frame_cache(storages)
                try:
                    # Operational failures must not become catalogue mapping gaps.
                    with Acquisition("finlab", credential=self._identity_credential) as operation:
                        try:
                            if self._sdk is None:
                                from findb_fetcher.provider_process import supervised_call

                                frame, observed = supervised_call(
                                    "finlab.get",
                                    {
                                        "token": self.api_token,
                                        "dataset": dataset.strip(),
                                        "symbols": reviewed_symbols,
                                        "account": account,
                                    },
                                    checkpoint=operation.dispatch_checkpoint,
                                )
                            else:
                                # Pinned 1.5.7 login only installs the token locally;
                                # one grant covers it and the dataset get I/O.
                                operation.dispatch_checkpoint()
                                login(self.api_token)
                                frame = get(dataset.strip())
                                observed = None
                        except QuotaBlockedError:
                            # __exit__ records whether get already dispatched;
                            # only the outer boundary may classify a quota wait.
                            raise
                        except Exception:
                            raise FinLabOperationalError(
                                "FinLab SDK dataset retrieval failed"
                            ) from None
                    if account:
                        try:
                            if observed is None:
                                observed = _dataframe_response_bytes(frame)
                            if permit:
                                permit.observe(observed)
                            if self.on_response_bytes is not None:
                                self.on_response_bytes(observed)
                        except Exception:
                            raise FinLabOperationalError(
                                "FinLab response accounting failed"
                            ) from None
                    try:
                        if target_date is None:
                            return _catalogue_from_dataframe(
                                frame, reviewed_symbols=reviewed_symbols
                            )
                        return _table_from_dataframe(
                            frame, target_date=target_date, reviewed_symbols=reviewed_symbols
                        )
                    except FinLabSdkError as exc:
                        raise type(exc)(str(exc)) from None
                    except Exception:
                        raise FinLabSdkError(
                            "FinLab SDK dataset result failed validation"
                        ) from None
                finally:
                    # get() can replace its storage while the package re-export
                    # still retains the old instance. Release both under the lock.
                    _disable_frame_cache((*storages, *_file_storages(data, get)))
        except QuotaBlockedError as exc:
            if exc.quota_wait and not exc.dispatched:
                raise
            raise FinLabOperationalError("FinLab acquisition failed") from None
        except FinLabSdkError:
            raise
        except Exception:
            # Never expose provider/credential context, including scope failures.
            raise FinLabOperationalError("FinLab acquisition failed") from None


def native_get(payload: dict[str, Any], authorize: Callable[[], None]) -> tuple[object, int | None]:
    """Worker-side pinned SDK dataset get with one fresh grant before native I/O.

    Returns the frame narrowed to reviewed columns and, when the parent accounts
    usage, the complete frame's response bytes. The full table never crosses
    the pipe, so the parent does not hold a second copy of it.
    """
    sdk = importlib.import_module("finlab")
    data = importlib.import_module("finlab.data")
    # check_version() issues an unbounded PyPI request on every fresh process;
    # the SDK version is pinned, so the check never informs acquisition.
    utils: Any = importlib.import_module("finlab.utils")
    utils.check_version = lambda: None
    get = data.get
    _disable_frame_cache(_file_storages(data, get))
    authorize()
    # Pinned 1.5.7 login only installs the token locally; get is the native I/O.
    sdk.login(payload["token"])
    frame = get(payload["dataset"])
    observed = _dataframe_response_bytes(frame) if payload["account"] else None
    return _reviewed_columns(frame, payload["symbols"]), observed


def _reviewed_columns(frame: Any, symbols: Sequence[str]) -> object:
    try:
        _frame_axes(frame)
        keep = frame.columns.isin(symbols)
    except Exception:
        # Not a valid pandas frame: return it whole so the parent raises the
        # same typed validation failure as the in-process path.
        return frame
    return frame.loc[:, keep]


def _file_storages(data: object, get: object) -> tuple[Any, ...]:
    """Resolve only the pinned SDK's concrete disk-backed storage instances."""
    storage_type = getattr(data, "FileStorage", None)
    if not isinstance(storage_type, type):
        return ()
    namespace = getattr(get, "__globals__", {})
    actual = namespace.get("_storage") if isinstance(namespace, dict) else None
    candidates = (getattr(data, "_storage", None), actual)
    return tuple(storage for storage in candidates if type(storage) is storage_type)


def _disable_frame_cache(storages: tuple[Any, ...]) -> None:
    for storage in storages:
        cache = getattr(storage, "_cache", None)
        if type(cache) is dict:
            # Native FileStorage still reads/writes its existing pickle files.
            # Do not call clear(): it deletes disk data. CacheStorage must retain
            # its memory-only data to avoid additional upstream downloads.
            storage.use_cache = False
            cache.clear()


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
    production_scope: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_datasets", MappingProxyType(dict(self.field_datasets)))
        object.__setattr__(self, "symbols", tuple(self.symbols))
        if _IDENTIFIER.fullmatch(self.dataset_key) is None:
            raise FinLabConfigError("dataset_key must be a lowercase identifier")
        if self.market != "TW":
            raise FinLabConfigError("FinLab v1 mapping supports only market TW")
        if self.asset_class != "equity" and not (
            self.production_scope and self.asset_class == "etf"
        ):
            raise FinLabConfigError("FinLab staging feed supports only asset_class equity")
        if self.dataset_key != {"equity": "tw_equity_eod", "etf": "tw_etf_eod"}[self.asset_class]:
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
    from findb_fetcher.provider_work import scoped_identity

    identity = hashlib.sha256(scoped_identity(bundle.raw_bytes)).hexdigest()
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


def _dataframe_response_bytes(frame: Any) -> int:
    """Count the complete columns JSON without retaining its full string/bytes copies."""
    if not frame.columns.is_unique or not frame.index.is_unique:
        raise FinLabSdkError("FinLab dataset axes must be unique")
    size = 2  # One outer object, including the zero-column case.
    for start in range(0, len(frame.columns), _FRAME_JSON_COLUMN_BATCH):
        fragment = frame.iloc[:, start : start + _FRAME_JSON_COLUMN_BATCH].to_json().encode("utf-8")
        if not fragment.startswith(b"{") or not fragment.endswith(b"}"):
            raise FinLabSdkError("FinLab dataset JSON representation is invalid")
        # Each default orient=columns slice owns complete column values. Replace
        # its outer braces with a comma between slices, preserving the exact size.
        size += len(fragment) - 2 + int(start > 0)
        del fragment
    return size


def _frame_axes(frame: object) -> tuple[tuple[object, ...], tuple[date, ...], tuple[str, ...]]:
    index = getattr(frame, "index", None)
    columns = getattr(frame, "columns", None)
    if index is None or columns is None:
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
    return index_values, dates, column_values


def _catalogue_from_dataframe(
    frame: object, *, reviewed_symbols: tuple[str, ...]
) -> FinLabDatasetTable:
    _, _, columns = _frame_axes(frame)
    present = set(columns)
    return FinLabDatasetTable(
        (), tuple(symbol for symbol in reviewed_symbols if symbol in present), ()
    )


def _table_from_dataframe(
    frame: object,
    *,
    target_date: date,
    reviewed_symbols: tuple[str, ...],
) -> FinLabDatasetTable:
    """Narrow a pandas-like FinLab frame to exactly one validated date row."""
    index_values, dates, column_values = _frame_axes(frame)
    loc = getattr(frame, "loc", None)
    if loc is None:
        raise FinLabSdkError("FinLab dataset result is not a DataFrame")
    missing_symbols = set(reviewed_symbols).difference(column_values)
    if missing_symbols:
        raise FinLabSdkError("FinLab dataset is missing reviewed symbols")
    matching = [position for position, value in enumerate(dates) if value == target_date]
    if len(matching) != 1:
        from findb_fetcher.provider_work import active

        if active() is None:
            raise FinLabTargetNotReadyError(
                "FinLab dataset does not contain exactly one target date"
            )
        # Only reviewed table data is captured, never DataFrame attrs or SDK
        # objects. This snapshot is negative evidence, not replay authority.
        try:
            rows = tuple(
                tuple(_snapshot_scalar(loc[index, symbol]) for symbol in reviewed_symbols)
                for index in index_values
            )
        except Exception:
            raise FinLabSdkError("FinLab dataset observation cannot be read") from None
        return FinLabDatasetTable(
            tuple(value.isoformat() for value in dates), reviewed_symbols, rows
        )
    try:
        row = tuple(loc[index_values[matching[0]], symbol] for symbol in reviewed_symbols)
    except Exception as exc:
        raise FinLabSdkError("FinLab dataset target row cannot be read") from exc
    return FinLabDatasetTable(
        dates=(target_date.isoformat(),),
        symbols=reviewed_symbols,
        values=(row,),
    )


def _snapshot_scalar(value: object) -> object:
    """Serialize only table scalar types, never arbitrary object repr/str."""
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, Decimal):
        return str(value)
    # NumPy scalar values returned by pandas expose item(); do not invoke an
    # arbitrary SDK object's method or stringify it into durable evidence.
    if type(value).__module__ == "numpy":
        return _snapshot_scalar(value.item())  # type: ignore[attr-defined]
    raise FinLabSdkError("FinLab dataset observation contains an unsupported scalar")


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
