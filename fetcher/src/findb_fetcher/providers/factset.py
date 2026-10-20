"""FactSet listing-level OHLCV acquisition through a bounded, fenced MySQL child."""

from __future__ import annotations

import json
import math
import os
import re
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from findb_fetcher.acquisition import Acquisition
from findb_fetcher.provider_process import supervised_call
from findb_fetcher.provider_work import durable_response

MAX_MEMBERS = 10000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
LISTING_ID = re.compile(r"^[A-Z0-9]{6}-L$")


class FactSetConfigError(ValueError):
    pass


class FactSetOperationalError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code, self.retryable = code, retryable


class FactSetRowError(ValueError):
    """A deterministic row failure; preserve native evidence and continue the cohort."""


@dataclass(frozen=True)
class FactSetConfig:
    host: str
    user: str = field(repr=False)
    password: str = field(repr=False)
    ca_file: Path
    port: int = 3306
    database: str = "factset"
    query_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if (
            not self.host
            or self.host != self.host.strip()
            or not re.fullmatch(r"[A-Za-z0-9.-]+", self.host)
            or not self.user
            or not self.password
            or not 1 <= self.port <= 65535
            or self.database != "factset"
            or not 1 <= self.query_timeout_seconds <= 60
            or not self.ca_file.is_file()
        ):
            raise FactSetConfigError("FactSet connection configuration is invalid")

    @classmethod
    def from_env(cls) -> FactSetConfig:
        try:
            return cls(
                host=os.environ["FACTSET_HOST"],
                user=os.environ["FACTSET_USER"],
                password=os.environ["FACTSET_PASSWORD"],
                ca_file=Path(os.environ["FACTSET_SSL_CA_FILE"]),
                port=int(os.getenv("FACTSET_PORT", "3306")),
                query_timeout_seconds=int(os.getenv("FACTSET_QUERY_TIMEOUT_SECONDS", "30")),
            )
        except (KeyError, ValueError):
            raise FactSetConfigError("FactSet connection configuration is unavailable") from None

    @property
    def account_identity(self) -> str:
        # Password rotation must not reset the account ledger.
        return f"{self.host.lower()}:{self.port}:{self.user}"


def _native_json(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    raise TypeError("unsupported FactSet native value")


def encode_native(rows: list[dict[str, Any]], *, scope: dict[str, Any]) -> bytes:
    # Preserve non-finite native observations as strings, never valid contract prices.
    detached = [
        {
            key: str(value) if isinstance(value, float) and not math.isfinite(value) else value
            for key, value in row.items()
        }
        for row in rows
    ]
    raw = json.dumps(
        {"scope": scope, "rows": detached},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_native_json,
        allow_nan=False,
    ).encode()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FactSetOperationalError("FACTSET_RESPONSE_TOO_LARGE", retryable=False)
    return raw


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, FactSetOperationalError):
        return exc.code
    code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
    return (
        "FACTSET_QUERY_REJECTED"
        if code in {1044, 1045, 1049, 1054, 1142, 1146} or isinstance(exc, ssl.SSLError)
        else "FACTSET_QUERY_UNAVAILABLE"
    )


class FactSetGateway:
    def __init__(self, config: FactSetConfig) -> None:
        self.config = config

    def _query(self, sql: str, parameters: tuple, *, scope: dict[str, Any]) -> bytes:
        config = self.config
        connection = None
        connection_error = None

        def connect() -> None:
            nonlocal connection
            import pymysql

            try:
                connection = pymysql.connect(
                    host=config.host,
                    port=config.port,
                    user=config.user,
                    password=config.password,
                    database=config.database,
                    ssl=ssl.create_default_context(cafile=str(config.ca_file)),
                    connect_timeout=10,
                    read_timeout=config.query_timeout_seconds + 5,
                    write_timeout=10,
                    autocommit=False,
                    cursorclass=pymysql.cursors.SSDictCursor,
                )
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SET SESSION MAX_EXECUTION_TIME=%s", (config.query_timeout_seconds * 1000,)
                    )
                    cursor.execute("START TRANSACTION READ ONLY")
            except Exception as exc:
                nonlocal connection_error
                connection_error = _safe_error(exc)

        def fetch() -> bytes | dict[str, str]:
            if connection_error is not None:
                return {"error": connection_error}
            assert connection is not None
            try:
                with connection.cursor() as cursor:
                    cursor.execute(sql, parameters)
                    rows = list(cursor.fetchmany(MAX_MEMBERS + 1))
                    if len(rows) > MAX_MEMBERS:
                        return {"error": "FACTSET_MEMBER_LIMIT"}
                    return encode_native(rows, scope=scope)
            except Exception as exc:
                return {"error": _safe_error(exc)}
            finally:
                try:
                    connection.rollback()
                finally:
                    connection.close()

        with Acquisition("factset", credential=config.account_identity) as operation:
            result = supervised_call(
                fetch,
                initialize=connect,
                checkpoint=operation.dispatch_checkpoint,
                timeout=config.query_timeout_seconds + 20,
            )
        if isinstance(result, dict):
            raise FactSetOperationalError(
                result["error"], retryable=result["error"] == "FACTSET_QUERY_UNAVAILABLE"
            )
        return result

    @durable_response("factset_catalogue", ("symbols",))
    def catalogue(self, symbols: tuple[str, ...]) -> bytes:
        if (
            not symbols
            or len(symbols) > MAX_MEMBERS
            or tuple(sorted(set(symbols))) != symbols
            or any(not re.fullmatch(r"[0-9]{5}", value) for value in symbols)
        ):
            raise FactSetConfigError("FactSet official symbol scope is invalid")
        tickers = tuple(f"{int(symbol)}-HKG" for symbol in symbols)
        placeholders = ",".join("%s" for _ in tickers)
        # Only the history table is indexed by ticker. Use it to locate candidate
        # listing IDs, then require the exact CURRENT ticker association. History
        # dates do not establish listing lifecycle or historical membership.
        return self._query(
            "SELECT DISTINCT c.fsym_id,c.currency,c.fref_security_type,c.listing_flag,t.ticker_exchange "
            "FROM sym_v1_sym_ticker_exchange_hist h "
            "STRAIGHT_JOIN sym_v1_sym_ticker_exchange t "
            "ON t.fsym_id=h.fsym_id AND t.ticker_exchange=h.ticker_exchange "
            "STRAIGHT_JOIN sym_v1_sym_coverage c ON c.fsym_id=t.fsym_id "
            f"WHERE h.ticker_exchange IN ({placeholders}) "
            "AND c.fref_listing_exchange=%s AND c.fref_security_type=%s AND c.listing_flag=1 "
            "ORDER BY t.ticker_exchange,c.fsym_id LIMIT 10001",
            (*tickers, "HKG", "SHARE"),
            scope={
                "kind": "catalogue",
                "exchange": "HKG",
                "security_type": "SHARE",
                "symbols": symbols,
            },
        )

    @durable_response("factset_prices", ("trade_date", "listing_ids"))
    def prices(self, trade_date: date, listing_ids: tuple[str, ...]) -> bytes:
        if (
            not listing_ids
            or len(listing_ids) > MAX_MEMBERS
            or tuple(sorted(set(listing_ids))) != listing_ids
            or any(not LISTING_ID.fullmatch(value) for value in listing_ids)
        ):
            raise FactSetConfigError("FactSet frozen listing scope is invalid")
        placeholders = ",".join("%s" for _ in listing_ids)
        return self._query(
            "SELECT fsym_id,price_date,currency,price,price_open,price_high,price_low,"
            "volume,turnover,trade_count,vwap FROM fgp_v1_fgp_global_prices "
            f"WHERE price_date=%s AND fsym_id IN ({placeholders}) "
            "ORDER BY fsym_id LIMIT 10001",
            (trade_date, *listing_ids),
            scope={
                "kind": "prices",
                "trade_date": trade_date.isoformat(),
                "listing_ids": listing_ids,
            },
        )


def map_current_cohort(official: list[dict[str, Any]], native: bytes) -> list[dict[str, Any]]:
    """Retain every official member, including missing or ambiguous provider mappings."""
    catalogue = json.loads(native)["rows"]
    candidates: dict[str, set[tuple[str, str]]] = {}
    for row in catalogue:
        ticker = row.get("ticker_exchange", "")
        if (
            not isinstance(ticker, str)
            or not re.fullmatch(r"[0-9]{1,5}-HKG", ticker)
            or row.get("listing_flag") != 1
            or row.get("fref_security_type") != "SHARE"
            or not LISTING_ID.fullmatch(str(row.get("fsym_id", "")))
        ):
            continue
        candidates.setdefault(ticker[:-4].zfill(5), set()).add(
            (row["fsym_id"], row.get("currency", ""))
        )
    mapped = []
    for original in official:
        row = dict(original)
        identities = candidates.get(row["symbol"], set())
        candidate = next(iter(identities)) if len(identities) == 1 else None
        valid = (
            candidate is not None
            and candidate[1] == row["currency"]
            and row.get("classification") != "classification_gap"
        )
        row["provider_symbol"] = candidate[0] if valid and candidate else row["symbol"]
        row["mapping_status"] = "mapped" if valid else "gap"
        mapped.append(row)
    return mapped


def contract_row(native: dict[str, Any], *, symbol: str, listing_id: str, day: date) -> dict:
    if (
        native.get("fsym_id") != listing_id
        or native.get("price_date") != day.isoformat()
        or not LISTING_ID.fullmatch(listing_id)
        or not re.fullmatch(r"[0-9]{5}", symbol)
    ):
        raise FactSetRowError("FACTSET_ROW_IDENTITY_INVALID")
    row: dict[str, Any] = {
        "symbol": symbol,
        "source_symbol": listing_id,
        "trade_date": day.isoformat(),
        "currency": native.get("currency"),
    }
    try:
        for target, source in (
            ("open", "price_open"),
            ("high", "price_high"),
            ("low", "price_low"),
            ("close", "price"),
            ("volume", "volume"),
        ):
            value = native.get(source)
            if value is None:
                if target == "close":
                    raise FactSetRowError("FACTSET_CLOSE_MISSING")
                row[target] = None
                continue
            if isinstance(value, bool):
                raise FactSetRowError("FACTSET_VALUE_INVALID")
            number = Decimal(str(value))
            if not number.is_finite() or number < 0:
                raise FactSetRowError("FACTSET_VALUE_INVALID")
            if target == "volume":
                if number != number.to_integral_value() or number > 2**63 - 1:
                    raise FactSetRowError("FACTSET_VOLUME_NOT_INTEGRAL")
                row[target] = int(number)
            else:
                if number >= Decimal("1000000000000"):
                    raise FactSetRowError("FACTSET_PRICE_OUT_OF_RANGE")
                row[target] = str(number)
    except (InvalidOperation, TypeError, ValueError) as exc:
        if isinstance(exc, FactSetRowError):
            raise
        raise FactSetRowError("FACTSET_VALUE_INVALID") from None
    if not isinstance(row["currency"], str) or not re.fullmatch(r"[A-Z0-9]{3,10}", row["currency"]):
        raise FactSetRowError("FACTSET_CURRENCY_INVALID")
    # Relational OHLC conflicts intentionally reach market_eod.v2 normalization.
    return row
