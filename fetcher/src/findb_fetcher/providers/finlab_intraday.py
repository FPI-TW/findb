"""FinLab intraday partitions: one symbol's trading day of minute bars or ticks.

FinLab publishes intraday data as one Parquet partition per symbol and trade
date, listed by a manifest that also says when a day had no trades. The worker
takes the provider's own bytes (kept as evidence) and maps them to the row
layout ``intraday_partition`` publishes, so nothing downstream reads FinLab's
column names. Neither file is written to the SDK's local cache or the work
journal: both go straight to raw storage.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from findb_fetcher.acquisition import QuotaBlockedError
from findb_fetcher.finlab_credentials import sdk_credential_scope
from findb_fetcher.providers.finlab import (
    FinLabConfigError,
    FinLabNativeError,
    FinLabOperationalError,
    FinLabSdkError,
    FinLabSdkGateway,
    _log_sdk_failure,
    _select_api_token,
)

# FinLab dataset family per record kind of the contract.
FAMILIES: Mapping[str, str] = {"bar_1m": "tw_minute", "tick": "tw_tick"}
# One manifest request, one signing request and one download of well under 1 MiB.
FINLAB_INTRADAY_TIMEOUT_SECONDS = 3 * 60
_SYMBOL = re.compile(r"^[0-9A-Z]{4,10}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# FinDB column <- FinLab column; ``symbol`` is the canonical symbol, not a column.
_COLUMN_SOURCES: Mapping[str, Mapping[str, str]] = {
    "bar_1m": {
        "trade_date": "trade_date",
        "bar_start_time": "timestamp",
        "session": "session",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
        "trade_count": "tick_count",
        "vwap": "vwap",
    },
    "tick": {
        "trade_date": "trade_date",
        "trade_time": "timestamp",
        "sequence": "sequence",
        "price": "close",
        "volume": "volume",
        "tick_type": "tick_type",
        "bid_price": "bid_price",
        "bid_volume": "bid_volume",
        "ask_price": "ask_price",
        "ask_volume": "ask_volume",
        "session": "session",
    },
}
_NOT_READY = {"not_ready_calendar": "calendar", "not_ready_partition": "partition"}


class FinLabIntradayNotReadyError(FinLabSdkError):
    """FinLab does not serve this symbol's trading day (yet).

    ``scope`` is ``calendar`` when FinLab has not verified the trade date at all,
    so no symbol can be served, and ``partition`` when the date is verified but
    this symbol has no partition: not built yet, or a symbol FinLab does not carry.
    The two cannot be told apart from one answer.
    """

    def __init__(self, scope: Literal["calendar", "partition"]) -> None:
        super().__init__(f"FinLab intraday {scope} is not ready")
        self.scope = scope


@dataclass(frozen=True, slots=True)
class IntradayPartition:
    """What FinLab says about one symbol's trading day, with its files when it traded.

    ``absent``: FinLab has verified the date but holds no partition for the symbol.
    It does so for a day without a board-lot trade, and also while a partition is
    still being built, so only Source can accept it as no trade.
    """

    status: Literal["ready", "no_trades", "absent"]
    # The manifest entry (or its absence) as bounded evidence; never a signed URL.
    manifest: Mapping[str, Any]
    row_count: int = 0
    # The provider's own Parquet file and the rows mapped to the contract layout.
    original: bytes = dataclass_field(default=b"", repr=False)
    rows: bytes = dataclass_field(default=b"", repr=False)


def fetch_partition(
    gateway: FinLabSdkGateway,
    *,
    record_kind: str,
    provider_symbol: str,
    symbol: str,
    trade_date: date,
    layout: Sequence[Mapping[str, Any]],
    manifest_only: bool = False,
) -> IntradayPartition:
    """Fetch one partition through the supervised worker, governed like a table get.

    With ``manifest_only`` the worker only asks whether the partition exists: a
    ready one comes back without its files, and nothing is downloaded or billed.
    """
    family = FAMILIES.get(record_kind)
    if (
        family is None
        or not isinstance(provider_symbol, str)
        or _SYMBOL.fullmatch(provider_symbol) is None
        or not isinstance(symbol, str)
        or not symbol
        or not isinstance(trade_date, date)
    ):
        raise FinLabConfigError("FinLab intraday request is invalid")
    dataset = f"{family}:{provider_symbol}"
    from findb_fetcher.acquisition import Acquisition
    from findb_fetcher.provider_process import ProviderProcessError, supervised_call
    from findb_fetcher.provider_worker import failure_code

    try:
        with (
            sdk_credential_scope(),
            Acquisition("finlab", credential=gateway._identity_credential) as operation,
        ):
            try:
                status, manifest, row_count, original, rows = supervised_call(
                    "finlab.intraday",
                    {
                        "token": gateway.api_token,
                        "record_kind": record_kind,
                        "provider_symbol": provider_symbol,
                        "symbol": symbol,
                        "trade_date": trade_date.isoformat(),
                        "layout": [dict(column) for column in layout],
                        "manifest_only": manifest_only,
                    },
                    checkpoint=operation.dispatch_checkpoint,
                    timeout=FINLAB_INTRADAY_TIMEOUT_SECONDS,
                )
            except QuotaBlockedError:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, ProviderProcessError) else failure_code(exc)
                if code in _NOT_READY:
                    raise FinLabIntradayNotReadyError(_NOT_READY[code]) from None  # type: ignore[arg-type]
                _log_sdk_failure(dataset, code)
                raise FinLabOperationalError("FinLab SDK intraday retrieval failed", code) from None
    except QuotaBlockedError as exc:
        if exc.quota_wait and not exc.dispatched:
            raise
        raise FinLabOperationalError("FinLab acquisition failed", "account_quota") from None
    except FinLabSdkError:
        raise
    except Exception:
        # Never expose provider/credential context, including scope failures.
        raise FinLabOperationalError("FinLab acquisition failed", "acquisition") from None
    if gateway.on_response_bytes is not None and original:
        try:
            gateway.on_response_bytes(len(original))
        except Exception:
            raise FinLabOperationalError("FinLab response accounting failed") from None
    return IntradayPartition(status, manifest, row_count, original, rows)


def native_intraday(
    payload: dict[str, Any], authorize: Callable[[], None]
) -> tuple[str, dict[str, Any], int, bytes, bytes]:
    """Worker-side manifest, download and mapping of one symbol's trading day.

    Returns ``(status, manifest entry, row count, provider file, mapped rows)``.
    A verified date without a partition for the symbol is the ``absent`` answer; a
    date FinLab has not verified, or a partition still being built, travel as the
    bounded codes ``not_ready_calendar`` and ``not_ready_partition``.
    """
    # Chosen before the SDK imports Arrow; see ``native_get``.
    os.environ.setdefault("ARROW_DEFAULT_MEMORY_POOL", "system")
    sdk = importlib.import_module("finlab")
    utils: Any = importlib.import_module("finlab.utils")
    utils.check_version = lambda: None
    seams = _intraday_seams(sdk)
    family = FAMILIES[payload["record_kind"]]
    provider_symbol, day = payload["provider_symbol"], payload["trade_date"]
    query = {"dataset": f"{family}:{provider_symbol}", "start": day, "end": day}
    authorize()
    _select_api_token(sdk, payload["token"])
    absent = {"date": day, "status": "absent"}
    try:
        try:
            manifest = _signed_request(seams, {**query, "mode": "manifest"})
        except FinLabNativeError as exc:
            if exc.native_failure_code == "not_ready_partition":
                return "absent", absent, 0, b"", b""
            raise
        parts = [part for part in manifest.get("partitions", ()) if part.get("date") == day]
        if not parts:
            if day in (manifest.get("closed_dates") or ()):
                # FinLab calls the day closed: a calendar disagreement, not a quiet symbol.
                raise FinLabNativeError("not_ready_partition")
            return "absent", absent, 0, b"", b""
        if len(parts) != 1:
            raise FinLabNativeError("bad_response")
        part = parts[0]
        evidence = _manifest_evidence(part, manifest)
        if part.get("status") == "no_trades":
            return "no_trades", evidence, 0, b"", b""
        if part.get("status") != "ready" or not _described(part):
            raise FinLabNativeError("not_ready_partition")
        if payload.get("manifest_only"):
            return "ready", evidence, int(part["rows"]), b"", b""
        signed = _signed_request(
            seams,
            {
                **query,
                "mode": "download",
                "request_id": uuid.uuid4().hex,
                "partitions": [{"date": day, "generation": part["generation"]}],
            },
        )
        by_date = {item.get("date"): item for item in signed.get("partitions", ())}
        original = seams.intraday._download(
            seams.intraday._verify_signed_partition(by_date.get(day), part)
        )
    except seams.quota_error:
        raise FinLabNativeError("quota") from None
    except seams.auth_error:
        raise FinLabNativeError("auth") from None
    except seams.data_error:
        # The SDK's own message can carry a signed URL; only the class survives.
        raise FinLabNativeError("bad_response") from None
    if len(original) != part["size"] or hashlib.sha256(original).hexdigest() != part["sha256"]:
        raise FinLabNativeError("bad_response")
    rows = map_rows(
        original,
        record_kind=payload["record_kind"],
        provider_symbol=provider_symbol,
        symbol=payload["symbol"],
        trade_date=day,
        row_count=part["rows"],
        layout=payload["layout"],
        source_columns=tuple(
            seams.intraday.TICK_COLUMNS if family == "tw_tick" else seams.intraday.BAR_COLUMNS
        ),
    )
    return "ready", evidence, int(part["rows"]), original, rows


def _described(part: Mapping[str, Any]) -> bool:
    return (
        isinstance(part.get("generation"), str)
        and bool(part["generation"])
        and isinstance(part.get("sha256"), str)
        and _SHA256.fullmatch(part["sha256"]) is not None
        and type(part.get("size")) is int
        and part["size"] > 0
        and type(part.get("rows")) is int
        and part["rows"] > 0
    )


def _manifest_evidence(part: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest fields that identify a published partition; nothing signed."""
    evidence = {
        name: part[name]
        for name in ("date", "status", "generation", "sha256", "size", "rows")
        if isinstance(part.get(name), (str, int)) and not isinstance(part.get(name), bool)
    }
    if isinstance(manifest.get("volume_unit"), str):
        evidence["volume_unit"] = manifest["volume_unit"]
    return evidence


def _signed_request(seams: _IntradaySeams, query: dict[str, Any]) -> dict[str, Any]:
    """One intraday API request, with FinLab's structured refusals kept apart.

    The SDK's own wrapper folds every refusal into one message; the fields that
    tell an unverified calendar from a missing partition are only in the reply.
    """
    result = seams.post("intraday", query, "Intraday")
    if not isinstance(result, Mapping):
        raise FinLabNativeError("bad_response")
    if "error" in result:
        code = result.get("code")
        if code == "not_ready":
            raise FinLabNativeError(
                "not_ready_calendar" if "available_through" in result else "not_ready_partition"
            )
        if isinstance(code, str) and "quota" in code:
            raise FinLabNativeError("quota")
        raise FinLabNativeError("bad_response")
    if result.get("schema_version") != 1 or result.get("dataset") != query["dataset"]:
        raise FinLabNativeError("bad_response")
    return dict(result)


@dataclass(frozen=True, slots=True)
class _IntradaySeams:
    """The pinned SDK's intraday names this worker relies on."""

    post: Callable[..., Any]
    intraday: Any
    quota_error: type[BaseException]
    auth_error: type[BaseException]
    data_error: type[BaseException]


def _intraday_seams(sdk: object) -> _IntradaySeams:
    """Fail closed when the installed SDK's intraday interface is not the reviewed one."""
    try:
        auth = importlib.import_module("finlab.data.auth")
        intraday = importlib.import_module("finlab.data.intraday")
        exceptions = importlib.import_module("finlab.exceptions")
    except ImportError:
        raise FinLabNativeError("sdk_contract") from None
    errors = [
        getattr(exceptions, name, None) for name in ("QuotaExceededError", "AuthError", "DataError")
    ]
    if not (
        callable(getattr(sdk, "login", None))
        and callable(getattr(sdk, "get_token", None))
        and callable(getattr(auth, "post_signing_request", None))
        and callable(getattr(intraday, "_download", None))
        and callable(getattr(intraday, "_verify_signed_partition", None))
        and all(
            isinstance(getattr(intraday, name, None), tuple)
            for name in ("BAR_COLUMNS", "TICK_COLUMNS")
        )
        and all(isinstance(error, type) and issubclass(error, BaseException) for error in errors)
    ):
        raise FinLabNativeError("sdk_contract")
    return _IntradaySeams(auth.post_signing_request, intraday, *errors)  # type: ignore[arg-type]


def verify_intraday_contract(contracts_dir: Path | None = None) -> None:
    """Offline check of the installed SDK's intraday interface and of the mapping.

    The second half matters across images: the installed Arrow must print the
    mapped columns exactly as the installed contract publishes them.
    """
    try:
        seams = _intraday_seams(importlib.import_module("finlab"))
    except FinLabNativeError:
        raise FinLabSdkError("FinLab SDK does not expose the reviewed intraday API") from None
    directory = contracts_dir or Path(os.getenv("FETCHER_CONTRACTS_DIR", "/app/contracts"))
    try:
        layouts = json.loads(
            (directory / "intraday_partition" / "v1.schema.json").read_text(encoding="utf-8")
        )["x-findb-row-layouts"]
        import pyarrow.parquet as pq

        for record_kind, family in FAMILIES.items():
            source_columns = tuple(
                seams.intraday.TICK_COLUMNS if family == "tw_tick" else seams.intraday.BAR_COLUMNS
            )
            layout = layouts[record_kind]["columns"]
            rows = map_rows(
                _sample_partition(source_columns),
                record_kind=record_kind,
                provider_symbol="2330",
                symbol="2330",
                trade_date="2026-01-02",
                row_count=1,
                layout=layout,
                source_columns=source_columns,
            )
            mapped = [
                (field.name, str(field.type), field.nullable)
                for field in pq.read_schema(io.BytesIO(rows))
            ]
            if mapped != [(c["name"], c["type"], c["nullable"]) for c in layout]:
                raise ValueError("mapped layout differs from the contract")
    except (FinLabNativeError, ImportError, OSError, KeyError, TypeError, ValueError):
        raise FinLabSdkError("FinLab intraday rows cannot be mapped to the contract") from None


def _sample_partition(source_columns: tuple[str, ...]) -> bytes:
    """One row shaped like a FinLab partition file, for the offline mapping check."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    integers = {"volume", "tick_count", "sequence", "ts", "bid_volume", "ask_volume"}
    values: dict[str, Any] = {}
    for name in source_columns:
        if name == "stock_id":
            values[name] = ["2330"]
        elif name == "trade_date":
            values[name] = ["2026-01-02"]
        elif name == "session":
            values[name] = ["regular"]
        elif name == "timestamp":
            values[name] = pa.array(
                [datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc)],
                type=pa.timestamp("ns", tz="Asia/Taipei"),
            )
        elif name == "tick_type":
            values[name] = pa.array([1], type=pa.int8())
        else:
            values[name] = [1] if name in integers else [1.0]
    sink = io.BytesIO()
    pq.write_table(pa.table(values), sink)
    return sink.getvalue()


def map_rows(
    original: bytes,
    *,
    record_kind: str,
    provider_symbol: str,
    symbol: str,
    trade_date: str,
    row_count: int,
    layout: Sequence[Mapping[str, Any]],
    source_columns: tuple[str, ...],
) -> bytes:
    """Map FinLab's partition file to the contract's row layout, as Parquet bytes.

    The file must be exactly what FinLab's manifest described: its columns, its
    row count, this symbol and this trade date only. Instants become UTC, the
    trade date a date, and the symbol the canonical one; values are not changed.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    types = {
        "string": pa.string(),
        "date32[day]": pa.date32(),
        "timestamp[us, tz=UTC]": pa.timestamp("us", tz="UTC"),
        "double": pa.float64(),
        "int64": pa.int64(),
        "int8": pa.int8(),
    }
    sources = _COLUMN_SOURCES[record_kind]
    try:
        table = pq.read_table(io.BytesIO(original))
        if tuple(table.column_names) != source_columns or table.num_rows != row_count:
            raise ValueError("partition file is not the manifest's")
        instant = table.schema.field("timestamp").type
        if not pa.types.is_timestamp(instant) or instant.tz is None:
            # A zone-less instant would be read as UTC and silently shift every row.
            raise ValueError("partition instants carry no time zone")
        if not (
            pc.all(pc.equal(table["stock_id"], provider_symbol)).as_py()
            and pc.all(pc.equal(pc.cast(table["trade_date"], pa.string()), trade_date)).as_py()
        ):
            raise ValueError("partition file holds another symbol or date")
        columns, fields = [], []
        for column in layout:
            name, kind = column["name"], types[column["type"]]
            if name == "symbol":
                values = pa.array([symbol] * table.num_rows, type=kind)
            elif name == "trade_date":
                values = pa.array([date.fromisoformat(trade_date)] * table.num_rows, type=kind)
            else:
                values = table[sources[name]].cast(kind)
            if not column["nullable"] and values.null_count:
                raise ValueError("required column has an empty value")
            columns.append(values)
            fields.append(pa.field(name, kind, nullable=bool(column["nullable"])))
        mapped = pa.Table.from_arrays(columns, schema=pa.schema(fields))
        sink = io.BytesIO()
        pq.write_table(mapped, sink, compression="zstd")
    except (pa.ArrowException, KeyError, TypeError, ValueError, OSError):
        raise FinLabNativeError("bad_response") from None
    return sink.getvalue()


def manifest_bytes(partition: IntradayPartition, *, dataset: str) -> bytes:
    """Canonical bytes of the manifest evidence, for raw storage."""
    return json.dumps(
        {"dataset": dataset, "manifest": dict(partition.manifest)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
