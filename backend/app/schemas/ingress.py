"""Provider-neutral, versioned ingress contracts."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Literal, NamedTuple, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.schemas.payload_limits import (
    ensure_data_items_count_within_limit,
    ensure_payload_size_within_limit,
)
from app.vocabulary import SOURCE_NAME_PATTERN

NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
FUTURES_CONTRACT_MONTH_PATTERN = r"[0-9]{4}(?:0[1-9]|1[0-2])(?:[WF][1-5])?"
CurrencyCode = Annotated[
    str,
    Field(min_length=3, max_length=10, pattern=r"^[A-Z0-9]+$"),
]


class DeliveryMode(str, Enum):
    """Completeness semantics declared by the fetch layer."""

    FULL_SNAPSHOT = "full_snapshot"
    INCREMENTAL = "incremental"
    BACKFILL = "backfill"


class IngressBatch(BaseModel):
    """Batch metadata shared by every versioned ingress payload."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    data_date: date
    delivery_mode: DeliveryMode
    declared_record_count: NonNegativeInt
    coverage_start_date: Optional[date] = None
    coverage_end_date: Optional[date] = None
    source_raw_ref: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    source_raw_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    sequence: Optional[int] = Field(default=None, ge=1)
    sequence_count: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_related_fields(self) -> "IngressBatch":
        if (self.sequence is None) != (self.sequence_count is None):
            raise ValueError("sequence and sequence_count must be provided together")
        if (
            self.sequence is not None
            and self.sequence_count is not None
            and self.sequence > self.sequence_count
        ):
            raise ValueError("sequence must be less than or equal to sequence_count")

        if (self.coverage_start_date is None) != (self.coverage_end_date is None):
            raise ValueError("coverage_start_date and coverage_end_date must be provided together")
        if (
            self.coverage_start_date is not None
            and self.coverage_end_date is not None
            and self.coverage_start_date > self.coverage_end_date
        ):
            raise ValueError("coverage_start_date must not be after coverage_end_date")
        if (
            self.delivery_mode == DeliveryMode.BACKFILL
            and self.coverage_end_date is not None
            and self.data_date != self.coverage_end_date
        ):
            raise ValueError("backfill data_date must equal coverage_end_date")
        return self


class IngressDeliveryMetadata(BaseModel):
    """Optional scheduler context retained for run-level observability."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slot_id: Literal[
        "western_markets_window",
        "global_markets_window",
        "taiwan_market_window",
        "asia_pacific_markets_window",
    ]
    scheduled_for: datetime
    target_data_date: date
    work_item_id: str = Field(min_length=1, max_length=100)

    @field_validator("scheduled_for")
    @classmethod
    def require_utc_aware_scheduled_for(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled_for must include a timezone")
        return value.astimezone(timezone.utc)


class _IngressRow(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class _OHLCRow(_IngressRow):
    open: Optional[NonNegativeDecimal] = None
    high: Optional[NonNegativeDecimal] = None
    low: Optional[NonNegativeDecimal] = None
    close: NonNegativeDecimal

    @model_validator(mode="after")
    def validate_ohlc_bounds(self) -> "_OHLCRow":
        comparable_values = [
            value for value in (self.open, self.low, self.close) if value is not None
        ]
        if self.high is not None and comparable_values and self.high < max(comparable_values):
            raise ValueError("high must not be below open, low, or close")

        comparable_values = [
            value for value in (self.open, self.high, self.close) if value is not None
        ]
        if self.low is not None and comparable_values and self.low > min(comparable_values):
            raise ValueError("low must not be above open, high, or close")
        return self


class MarketEODRow(_OHLCRow):
    """Provider-neutral daily OHLCV row."""

    symbol: str = Field(min_length=1, max_length=50)
    source_symbol: Optional[str] = Field(default=None, min_length=1, max_length=100)
    trade_date: date
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    currency: Optional[CurrencyCode] = None
    volume: Optional[NonNegativeInt] = None
    turnover: Optional[NonNegativeDecimal] = None
    total_ticks: Optional[NonNegativeInt] = None


class MarketEODRowV2(MarketEODRow):
    """The v1 fields, with relational OHLC checks deferred to row-level DQ."""

    @model_validator(mode="after")
    def validate_ohlc_bounds(self) -> "MarketEODRowV2":
        # Override only the inherited relational validator. Field constraints,
        # including required close and non-negative prices, remain unchanged.
        return self


def _validate_payload_batch(batch: IngressBatch, data: Sequence[MarketEODRow]) -> None:
    ensure_data_items_count_within_limit(len(data))
    if batch.declared_record_count != len(data):
        raise PydanticCustomError(
            "declared_record_count_mismatch",
            "declared_record_count must equal the number of data rows",
            {
                "declared_record_count": batch.declared_record_count,
                "actual_record_count": len(data),
            },
        )

    row_dates = [row.trade_date for row in data]
    if not row_dates:
        return

    minimum_date = min(row_dates)
    maximum_date = max(row_dates)
    contains_other_dates = minimum_date != batch.data_date or maximum_date != batch.data_date
    if contains_other_dates:
        if batch.coverage_start_date is None or batch.coverage_end_date is None:
            raise ValueError("coverage dates are required when data spans beyond data_date")
        if batch.coverage_start_date > minimum_date or batch.coverage_end_date < maximum_date:
            raise ValueError("coverage dates must include every row trade_date")


class MarketEODPayload(BaseModel):
    """Payload body for ``market_eod.v1``."""

    model_config = ConfigDict(extra="forbid")

    batch: IngressBatch
    data: list[MarketEODRow]

    @model_validator(mode="after")
    def validate_batch_and_rows(self) -> "MarketEODPayload":
        _validate_payload_batch(self.batch, self.data)
        natural_keys = [(row.symbol, row.trade_date) for row in self.data]
        if len(natural_keys) != len(set(natural_keys)):
            raise PydanticCustomError(
                "duplicate_delivery_key",
                "duplicate (symbol, trade_date) rows are not allowed",
            )
        ensure_payload_size_within_limit(self.model_dump(mode="json"))
        return self


class MarketEODPayloadV2(BaseModel):
    """Payload body for ``market_eod.v2``; invalid OHLC relationships reach DQ."""

    model_config = ConfigDict(extra="forbid")

    batch: IngressBatch
    data: list[MarketEODRowV2]

    @model_validator(mode="after")
    def validate_batch_and_rows(self) -> "MarketEODPayloadV2":
        _validate_payload_batch(self.batch, self.data)
        natural_keys = [(row.symbol, row.trade_date) for row in self.data]
        if len(natural_keys) != len(set(natural_keys)):
            raise PydanticCustomError(
                "duplicate_delivery_key",
                "duplicate (symbol, trade_date) rows are not allowed",
            )
        ensure_payload_size_within_limit(self.model_dump(mode="json"))
        return self


class FuturesEODRow(_IngressRow):
    """An actual TAIFEX expiry and exchange-attributed trading session."""

    product_code: Literal["TX", "MTX", "TMF", "TE", "TF"]
    contract_code: str = Field(
        min_length=2,
        max_length=50,
        pattern=rf"^(TX|MTX|TMF|TE|TF):{FUTURES_CONTRACT_MONTH_PATTERN}$",
    )
    contract_month: str = Field(
        min_length=2, max_length=10, pattern=rf"^{FUTURES_CONTRACT_MONTH_PATTERN}$"
    )
    trade_date: date
    session: Literal["regular", "after_hours"]
    open: Optional[NonNegativeDecimal] = None
    high: Optional[NonNegativeDecimal] = None
    low: Optional[NonNegativeDecimal] = None
    close: Optional[NonNegativeDecimal] = None
    volume: Optional[NonNegativeInt] = None
    settlement_price: Optional[NonNegativeDecimal] = None
    open_interest: Optional[NonNegativeInt] = None

    @model_validator(mode="after")
    def validate_prices(self) -> "FuturesEODRow":
        if self.contract_code != f"{self.product_code}:{self.contract_month}":
            raise ValueError("contract_code must preserve product and source contract month")
        values = [
            value for value in (self.open, self.high, self.low, self.close) if value is not None
        ]
        if self.high is not None and values and self.high < max(values):
            raise ValueError("high must not be below another OHLC value")
        if self.low is not None and values and self.low > min(values):
            raise ValueError("low must not be above another OHLC value")
        if (
            not values
            and self.settlement_price is None
            and self.open_interest is None
            and not (self.volume is not None and self.volume > 0)
        ):
            raise ValueError("futures quote must contain a source-provided observation")
        return self


class FuturesEODPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch: IngressBatch
    data: list[FuturesEODRow]

    @model_validator(mode="after")
    def validate_batch_and_rows(self) -> "FuturesEODPayload":
        ensure_data_items_count_within_limit(len(self.data))
        if self.batch.declared_record_count != len(self.data):
            raise PydanticCustomError(
                "declared_record_count_mismatch",
                "declared_record_count must equal the number of data rows",
            )
        keys = [
            (row.product_code, row.contract_code, row.trade_date, row.session) for row in self.data
        ]
        if len(keys) != len(set(keys)):
            raise PydanticCustomError(
                "duplicate_delivery_key",
                "duplicate futures contract/date/session rows are not allowed",
            )
        dates = [row.trade_date for row in self.data]
        if dates and (min(dates) != self.batch.data_date or max(dates) != self.batch.data_date):
            if self.batch.coverage_start_date is None or self.batch.coverage_end_date is None:
                raise ValueError("coverage dates are required for multi-date futures batches")
            if self.batch.coverage_start_date > min(dates) or self.batch.coverage_end_date < max(
                dates
            ):
                raise ValueError("coverage dates must include every futures trade_date")
        ensure_payload_size_within_limit(self.model_dump(mode="json"))
        return self


class MinuteSymbolOutcome(str, Enum):
    DATA = "data"
    EXPECTED_NO_DATA = "expected_no_data"
    ERROR = "error"


class MinuteSymbolStatus(_IngressRow):
    """Credential-free completeness outcome for a requested symbol."""

    symbol: str = Field(min_length=1, max_length=50)
    outcome: MinuteSymbolOutcome
    reason: Optional[str] = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_reason_for_non_data(self) -> "MinuteSymbolStatus":
        if self.outcome is MinuteSymbolOutcome.DATA and self.reason is not None:
            raise ValueError("data outcome must not include a reason")
        if self.outcome is not MinuteSymbolOutcome.DATA and self.reason is None:
            raise ValueError("non-data outcome requires a reason")
        return self


class ProviderUsageSnapshot(_IngressRow):
    """Provider usage counters deliberately exclude account and credential details."""

    requests_used: NonNegativeInt
    requests_limit: Optional[NonNegativeInt] = None

    @model_validator(mode="after")
    def validate_limit(self) -> "ProviderUsageSnapshot":
        if self.requests_limit is not None and self.requests_used > self.requests_limit:
            raise ValueError("requests_used must not exceed requests_limit")
        return self


class ProviderFieldAnomaly(_IngressRow):
    """Adapter observation for invalid provider volume/turnover values."""

    symbol: str = Field(min_length=1, max_length=50)
    bar_start_time: datetime
    field: Literal["volume", "turnover"]
    raw_value: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    raw_value_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("bar_start_time")
    @classmethod
    def require_aware_bar_start_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("anomaly bar_start_time must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_raw_value_checksum(self) -> "ProviderFieldAnomaly":
        import hashlib

        if self.raw_value_sha256 != hashlib.sha256(self.raw_value.encode("utf-8")).hexdigest():
            raise ValueError("raw_value_sha256 must match raw_value")
        return self


class MarketMinuteRow(_OHLCRow):
    """A Taiwan one-minute OHLC bar in canonical ingress units."""

    open: NonNegativeDecimal
    high: NonNegativeDecimal
    low: NonNegativeDecimal
    close: NonNegativeDecimal
    symbol: str = Field(min_length=1, max_length=50)
    source_symbol: Optional[str] = Field(default=None, min_length=1, max_length=100)
    trade_date: date
    market_timezone: Literal["Asia/Taipei"]
    bar_start_time: datetime
    bar_end_time: datetime
    signal_time: datetime
    price_adjustment: Literal["none"]
    trade_count: Literal[None] = None
    volume: Optional[NonNegativeInt] = None
    turnover: Optional[NonNegativeDecimal] = None

    @field_validator("bar_start_time", "bar_end_time", "signal_time")
    @classmethod
    def require_aware_utc_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("minute timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_one_minute_bar(self) -> "MarketMinuteRow":

        if self.bar_end_time != self.bar_start_time + timedelta(minutes=1):
            raise ValueError("bar_end_time must equal bar_start_time plus one minute")
        if self.signal_time != self.bar_end_time:
            raise ValueError("signal_time must equal bar_end_time")
        if self.bar_start_time.astimezone(ZoneInfo("Asia/Taipei")).date() != self.trade_date:
            raise ValueError("trade_date must match the Taiwan-local bar_start_time date")
        return self


class MarketMinuteBatch(IngressBatch):
    """Required sequence identity for Taiwan minute snapshot deliveries."""

    coverage_start_date: date
    coverage_end_date: date
    # Pydantic intentionally narrows this discriminator beyond the EOD-only base enum.
    delivery_mode: Literal["sequenced_snapshot"]  # type: ignore[assignment]
    snapshot_id: str = Field(min_length=1, max_length=100)
    daily_update_id: str = Field(min_length=1, max_length=100)
    universe_id: str = Field(min_length=1, max_length=100)
    symbols_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=1, le=99_999)
    sequence_count: int = Field(ge=1, le=99_999)
    provider_usage_before: Optional[ProviderUsageSnapshot] = None
    provider_usage_after: Optional[ProviderUsageSnapshot] = None
    anomalies: list[ProviderFieldAnomaly] = Field(default_factory=list, max_length=15_000)

    @model_validator(mode="after")
    def validate_minute_coverage(self) -> "MarketMinuteBatch":
        if self.coverage_start_date != self.data_date or self.coverage_end_date != self.data_date:
            raise ValueError("minute coverage must equal data_date")
        if (self.provider_usage_before is None) != (self.provider_usage_after is None):
            raise ValueError(
                "provider_usage_before and provider_usage_after must be provided together"
            )
        if (
            self.provider_usage_before is not None
            and self.provider_usage_after is not None
            and (
                self.provider_usage_after.requests_used < self.provider_usage_before.requests_used
                or self.provider_usage_after.requests_limit
                != self.provider_usage_before.requests_limit
            )
        ):
            raise ValueError(
                "provider usage must not decrease and requests_limit must remain stable"
            )
        return self


class MarketMinutePayload(BaseModel):
    """``market_minute.v1`` body normalized into canonical minute bars."""

    model_config = ConfigDict(extra="forbid")

    batch: MarketMinuteBatch
    symbol_statuses: list[MinuteSymbolStatus] = Field(min_length=1, max_length=50)
    data: list[MarketMinuteRow] = Field(max_length=15_000)

    @model_validator(mode="after")
    def validate_batch_and_rows(self) -> "MarketMinutePayload":
        import hashlib

        ensure_data_items_count_within_limit(len(self.data))
        if self.batch.declared_record_count != len(self.data):
            raise PydanticCustomError(
                "declared_record_count_mismatch",
                "declared_record_count must equal the number of data rows",
            )
        status_by_symbol = {status.symbol: status for status in self.symbol_statuses}
        if len(status_by_symbol) != len(self.symbol_statuses):
            raise PydanticCustomError(
                "duplicate_symbol_status", "symbol_statuses must not repeat a symbol"
            )
        expected_checksum = hashlib.sha256(
            "\n".join(sorted(status_by_symbol)).encode("utf-8")
        ).hexdigest()
        if self.batch.symbols_sha256 != expected_checksum:
            raise ValueError("symbols_sha256 must match sorted symbol_statuses symbols")
        natural_keys = [(row.symbol, row.bar_start_time) for row in self.data]
        if len(natural_keys) != len(set(natural_keys)):
            raise PydanticCustomError(
                "duplicate_delivery_key", "duplicate (symbol, bar_start_time) rows are not allowed"
            )
        for row in self.data:
            status = status_by_symbol.get(row.symbol)
            if status is None or status.outcome is not MinuteSymbolOutcome.DATA:
                raise ValueError("every minute row requires a matching data symbol status")
            if not self.batch.coverage_start_date <= row.trade_date <= self.batch.coverage_end_date:
                raise ValueError("coverage dates must include every row trade_date")
        data_symbols = {row.symbol for row in self.data}
        if any(
            status.outcome is MinuteSymbolOutcome.DATA and status.symbol not in data_symbols
            for status in self.symbol_statuses
        ):
            raise ValueError("data symbol status requires at least one row")
        rows_by_key = {(row.symbol, row.bar_start_time): row for row in self.data}
        anomaly_keys = [
            (anomaly.symbol, anomaly.bar_start_time, anomaly.field)
            for anomaly in self.batch.anomalies
        ]
        if len(anomaly_keys) != len(set(anomaly_keys)):
            raise ValueError("anomalies must not repeat a row field")
        for anomaly in self.batch.anomalies:
            referenced_row = rows_by_key.get((anomaly.symbol, anomaly.bar_start_time))
            if referenced_row is None:
                raise ValueError("anomaly must reference an existing (symbol, bar_start_time) row")
            if getattr(referenced_row, anomaly.field) is not None:
                raise ValueError("anomaly field must be null on its referenced row")
        anomaly_key_set = set(anomaly_keys)
        for row in self.data:
            for field in ("volume", "turnover"):
                if (
                    getattr(row, field) is None
                    and (
                        row.symbol,
                        row.bar_start_time,
                        field,
                    )
                    not in anomaly_key_set
                ):
                    raise ValueError(f"null {field} requires exactly one anomaly evidence record")
        ensure_payload_size_within_limit(self.model_dump(mode="json"))
        return self


# A stored object is named without credentials, query, encoded or dot segments.
RAW_OBJECT_REF_PATTERN = (
    r"^r2://[0-9a-f]{32}/[a-z0-9][a-z0-9.-]{1,61}[a-z0-9](/[A-Za-z0-9_=-][A-Za-z0-9._=-]*)+$"
)
# The symbol becomes one segment of the canonical object key.
INTRADAY_SYMBOL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$"
# Worker memory bound for one symbol's day; the largest sampled tick day is under 1 MiB.
INTRADAY_MAX_ROWS_BYTES = 16 * 1024 * 1024
INTRADAY_MAX_ROWS = 10_000_000
MINUTES_PER_DAY = 1440


class IntradayColumn(NamedTuple):
    """One column of a rows object; ``type`` is the Arrow type as pyarrow prints it."""

    name: str
    type: str
    nullable: bool
    description: str


# The producer maps its provider's fields to these columns, in this order, before
# delivery; FinDB reads no other layout.  Instants are UTC; ``trade_date`` is the
# exchange-local date; prices are unadjusted and volumes are in lots.
INTRADAY_ROW_LAYOUTS: Mapping[str, tuple[IntradayColumn, ...]] = MappingProxyType(
    {
        "bar_1m": (
            IntradayColumn("symbol", "string", False, "canonical symbol, leading zeros kept"),
            IntradayColumn("trade_date", "date32[day]", False, "exchange-local trading date"),
            IntradayColumn("bar_start_time", "timestamp[us, tz=UTC]", False, "start of the minute"),
            IntradayColumn("session", "string", False, "trading session, e.g. regular"),
            IntradayColumn("open", "double", False, "unadjusted"),
            IntradayColumn("high", "double", False, "unadjusted"),
            IntradayColumn("low", "double", False, "unadjusted"),
            IntradayColumn("close", "double", False, "unadjusted"),
            IntradayColumn("volume", "int64", False, "lots traded in the minute"),
            IntradayColumn("trade_count", "int64", True, "trades in the minute"),
            IntradayColumn("vwap", "double", True, "provider volume-weighted average price"),
        ),
        "tick": (
            IntradayColumn("symbol", "string", False, "canonical symbol, leading zeros kept"),
            IntradayColumn("trade_date", "date32[day]", False, "exchange-local trading date"),
            IntradayColumn("trade_time", "timestamp[us, tz=UTC]", False, "time of the trade"),
            IntradayColumn("sequence", "int64", False, "provider sequence within the day"),
            IntradayColumn("price", "double", False, "unadjusted trade price"),
            IntradayColumn("volume", "int64", False, "lots traded"),
            IntradayColumn("tick_type", "int8", True, "provider trade-direction code, as given"),
            IntradayColumn("bid_price", "double", True, "best bid at the trade"),
            IntradayColumn("bid_volume", "int64", True, "lots at the best bid"),
            IntradayColumn("ask_price", "double", True, "best ask at the trade"),
            IntradayColumn("ask_volume", "int64", True, "lots at the best ask"),
            IntradayColumn("session", "string", False, "e.g. regular, after_hours_fixed"),
        ),
    }
)


class IntradayPartitionRow(_IngressRow):
    """One symbol's complete trading day, described but not carried.

    The rows are in a stored object the producer already mapped to the layout
    of ``record_kind`` (``INTRADAY_ROW_LAYOUTS``).  Its size, checksum and row
    count are proven again before the object is used.
    """

    symbol: str = Field(pattern=INTRADAY_SYMBOL_PATTERN)
    source_symbol: Optional[str] = Field(default=None, min_length=1, max_length=100)
    trade_date: date
    record_kind: Literal["bar_1m", "tick"]
    row_count: int = Field(ge=1, le=INTRADAY_MAX_ROWS)
    rows_ref: str = Field(max_length=2048, pattern=RAW_OBJECT_REF_PATTERN)
    rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows_bytes: int = Field(ge=1, le=INTRADAY_MAX_ROWS_BYTES)
    rows_format: Literal["parquet"]
    price_adjustment: Literal["none"]
    # One lot is the exchange board lot (張): 1,000 shares for TWSE/TPEx stocks and ETFs.
    volume_unit: Literal["lot"]


class IntradayPartitionBatch(IngressBatch):
    """A by-reference delivery: provider evidence is mandatory, sequencing is absent."""

    delivery_mode: Literal["incremental", "backfill"]  # type: ignore[assignment]
    declared_record_count: Literal[1]  # type: ignore[assignment]
    # The provider's own file for this day, kept as evidence; never parsed by FinDB.
    source_raw_ref: str = Field(max_length=2048, pattern=RAW_OBJECT_REF_PATTERN)
    source_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: None = None
    sequence_count: None = None


class IntradayPartitionPayload(BaseModel):
    """Payload body for ``intraday_partition.v1``: exactly one symbol and trade date."""

    model_config = ConfigDict(extra="forbid")

    batch: IntradayPartitionBatch
    data: list[IntradayPartitionRow] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def validate_single_trading_day(self) -> "IntradayPartitionPayload":
        partition = self.data[0]
        if partition.trade_date != self.batch.data_date:
            raise ValueError("partition trade_date must equal batch data_date")
        if self.batch.coverage_start_date not in (None, self.batch.data_date) or (
            self.batch.coverage_end_date not in (None, self.batch.data_date)
        ):
            raise ValueError("partition coverage must be the single batch data_date")
        if partition.record_kind == "bar_1m" and partition.row_count > MINUTES_PER_DAY:
            raise ValueError("a one-minute bar partition cannot exceed the minutes in a day")
        ensure_payload_size_within_limit(self.model_dump(mode="json"))
        return self


# An exchange report the producer stored gzip-compressed; FinDB keeps it as evidence.
EXCHANGE_REPORT_MAX_BYTES = 64 * 1024 * 1024
EXCHANGE_REPORT_GZIP_MAX_BYTES = 16 * 1024 * 1024
EXCHANGE_SYMBOL_PATTERN = r"^[0-9]{5}$"


class ExchangeReportEvidence(BaseModel):
    """The exchange's own report the rows were read from, as the exchange served it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # The HKEX daily quotation covers the Main Board; GEM has a report of its own.
    board: Literal["main", "gem"]
    url: str = Field(
        max_length=200,
        pattern=r"^https://www\.hkex\.com\.hk/eng/stat/smstat/dayquot/[A-Za-z0-9_/]+\.htm$",
    )
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_bytes: int = Field(ge=1, le=EXCHANGE_REPORT_MAX_BYTES)
    gzip_bytes: int = Field(ge=1, le=EXCHANGE_REPORT_GZIP_MAX_BYTES)
    last_modified: datetime
    etag: str = Field(min_length=1, max_length=100)

    @field_validator("last_modified")
    @classmethod
    def require_utc_aware_last_modified(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_modified must include a timezone")
        return value.astimezone(timezone.utc)


class ExchangeDailyQuoteRow(_IngressRow):
    """One listed security's line in the exchange's daily quotation, as published.

    Relations between the values are data-quality rules, not shape: the exchange's
    close is not always a traded price, and odd-lot trades sit outside high/low.
    """

    symbol: str = Field(pattern=EXCHANGE_SYMBOL_PATTERN)
    trade_date: date
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    currency: CurrencyCode
    trading_status: Literal["traded", "no_trade"]
    previous_close: Optional[NonNegativeDecimal] = None
    close: Optional[NonNegativeDecimal] = None
    ask: Optional[NonNegativeDecimal] = None
    bid: Optional[NonNegativeDecimal] = None
    high: Optional[NonNegativeDecimal] = None
    low: Optional[NonNegativeDecimal] = None
    volume: NonNegativeInt
    turnover: NonNegativeDecimal

    @model_validator(mode="after")
    def status_matches_volume(self) -> "ExchangeDailyQuoteRow":
        if (self.trading_status == "traded") != (self.volume > 0):
            raise ValueError("trading_status is traded exactly when volume is positive")
        return self


class ExchangeDailyQuoteBatch(IngressBatch):
    """A by-reference delivery: the gzip-compressed report is mandatory evidence."""

    delivery_mode: Literal["incremental", "backfill"]  # type: ignore[assignment]
    source_raw_ref: str = Field(max_length=2048, pattern=RAW_OBJECT_REF_PATTERN)
    source_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report: ExchangeReportEvidence
    sequence: None = None
    sequence_count: None = None


class ExchangeDailyQuotePayload(BaseModel):
    """Payload body for ``exchange_daily_quote.v1``: rows of one report and trading day."""

    model_config = ConfigDict(extra="forbid")

    batch: ExchangeDailyQuoteBatch
    data: list[ExchangeDailyQuoteRow] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_single_report_day(self) -> "ExchangeDailyQuotePayload":
        ensure_data_items_count_within_limit(len(self.data))
        if self.batch.declared_record_count != len(self.data):
            raise PydanticCustomError(
                "declared_record_count_mismatch",
                "declared_record_count must equal the number of data rows",
            )
        if any(row.trade_date != self.batch.data_date for row in self.data):
            raise ValueError("every row trade_date must equal batch data_date")
        if self.batch.coverage_start_date not in (None, self.batch.data_date) or (
            self.batch.coverage_end_date not in (None, self.batch.data_date)
        ):
            raise ValueError("report coverage must be the single batch data_date")
        if len({row.symbol for row in self.data}) != len(self.data):
            raise PydanticCustomError(
                "duplicate_delivery_key",
                "duplicate (symbol, trade_date) rows are not allowed",
            )
        ensure_payload_size_within_limit(self.model_dump(mode="json"))
        return self


# Index reference documents: a publisher's own files, kept as evidence.
# Each index reference contract and the publisher document kind it carries.
INDEX_REFERENCE_SCHEMAS: Mapping[str, str] = MappingProxyType(
    {
        "index_constituents": "constituent_report",
        "index_constituent_changes": "change_workbook",
    }
)
INDEX_CODE_PATTERN = r"^[0-9]{5}\.[0-9]{2}$"
INDEX_DOCUMENT_URL_PATTERN = r"^https://www\.hsi\.com\.hk/[A-Za-z0-9_./?=%&-]+$"
INDEX_PART_KEY_PATTERN = r"^(?:[0-9]{5}\.[0-9]{2}|document)$"
# Bounded to the stored precision, so a stored value is the published one.
IndexWeight = Annotated[Decimal, Field(ge=0, le=100, decimal_places=4)]
IndexPrice = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=6)]
IndexText = Annotated[str, Field(min_length=1, max_length=200)]


class IndexDocumentEvidence(BaseModel):
    """The publisher's document the rows were read from, as the publisher served it.

    ``document_date`` is the date the content speaks for: a report's trading
    day or a workbook's latest effective date, in the publisher's local time.  ``part_key`` names the part of the document
    this delivery carries (an index code), or ``document`` for all of it.  The
    publisher's download service sends no ETag and often no Last-Modified.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    publisher: Literal["hsil"]
    kind: Literal["constituent_report", "change_workbook"]
    series: Literal["hsi", "hsci"]
    url: str = Field(max_length=500, pattern=INDEX_DOCUMENT_URL_PATTERN)
    document_date: date
    part_key: str = Field(pattern=INDEX_PART_KEY_PATTERN)
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_bytes: int = Field(ge=1, le=EXCHANGE_REPORT_MAX_BYTES)
    gzip_bytes: int = Field(ge=1, le=EXCHANGE_REPORT_GZIP_MAX_BYTES)
    last_modified: Optional[datetime] = None
    etag: Optional[str] = Field(default=None, min_length=1, max_length=100)

    @field_validator("last_modified")
    @classmethod
    def require_utc_aware_last_modified(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_modified must include a timezone")
        return value.astimezone(timezone.utc)


class IndexClassification(BaseModel):
    """The industry an industry index stands for, in the publisher's scheme."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scheme: Literal["hsics"]
    code: str = Field(pattern=r"^[0-9]{2}$")
    name_en: str = Field(min_length=1, max_length=100)
    name_zh: Optional[str] = Field(default=None, min_length=1, max_length=100)


class IndexDescriptor(BaseModel):
    """An index as the document names it; an industry index carries its industry."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(pattern=INDEX_CODE_PATTERN)
    name_en: IndexText
    name_zh: Optional[IndexText] = None
    parent_code: Optional[str] = Field(default=None, pattern=INDEX_CODE_PATTERN)
    classification: Optional[IndexClassification] = None

    @model_validator(mode="after")
    def parent_is_another_index(self) -> "IndexDescriptor":
        if self.parent_code == self.code:
            raise ValueError("an index is not its own parent")
        return self


class IndexDocumentBatch(IngressBatch):
    """A complete document part: the gzip-compressed document is mandatory evidence.

    ``data_date`` is the publisher-local date the producer observed the document;
    the content's own date is ``document.document_date``.
    """

    delivery_mode: Literal["full_snapshot"]  # type: ignore[assignment]
    source_raw_ref: str = Field(max_length=2048, pattern=RAW_OBJECT_REF_PATTERN)
    source_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document: IndexDocumentEvidence
    sequence: None = None
    sequence_count: None = None
    coverage_start_date: None = None  # type: ignore[assignment]
    coverage_end_date: None = None  # type: ignore[assignment]

    @model_validator(mode="after")
    def document_is_not_from_the_future(self) -> "IndexDocumentBatch":
        if self.document.document_date > self.data_date:
            raise ValueError("document_date must not follow the observation data_date")
        return self


def _require_root_parents(indexes: Sequence["IndexDescriptor"]) -> None:
    """A parent is another index the document names, and is itself a top-level index."""
    parents = {index.code: index.parent_code for index in indexes}
    for index in indexes:
        if index.parent_code is None:
            continue
        if index.parent_code not in parents:
            raise ValueError("a parent index must be one the document lists")
        if parents[index.parent_code] is not None:
            raise ValueError("a parent index must itself be a top-level index")


def _require_declared_rows(batch: IngressBatch, rows: Sequence[object]) -> None:
    ensure_data_items_count_within_limit(len(rows))
    if batch.declared_record_count != len(rows):
        raise PydanticCustomError(
            "declared_record_count_mismatch",
            "declared_record_count must equal the number of data rows",
        )


class IndexConstituentCount(IndexDescriptor):
    """An index a constituent report lists, with how many constituents it lists."""

    constituent_count: int = Field(ge=1)


class IndexConstituentRow(_IngressRow):
    """One constituent's line in a constituent report, as published."""

    index_code: str = Field(pattern=INDEX_CODE_PATTERN)
    trade_date: date
    symbol: str = Field(pattern=EXCHANGE_SYMBOL_PATTERN)
    name_en: Optional[IndexText] = None
    name_zh: Optional[IndexText] = None
    listing_place: str = Field(min_length=1, max_length=50)
    currency: CurrencyCode
    close: Optional[IndexPrice] = None
    weight_pct: IndexWeight
    # The report's own industry column; membership of an industry index decides.
    industry: Optional[IndexClassification] = None


class IndexConstituentsBatch(IndexDocumentBatch):
    indexes: list[IndexConstituentCount] = Field(min_length=1, max_length=50)


class IndexConstituentsPayload(BaseModel):
    """Payload body for ``index_constituents.v1``: one report's complete constituent lists."""

    model_config = ConfigDict(extra="forbid")

    batch: IndexConstituentsBatch
    data: list[IndexConstituentRow] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_lists(self) -> "IndexConstituentsPayload":
        batch = self.batch
        _require_declared_rows(batch, self.data)
        if batch.document.kind != "constituent_report" or batch.document.part_key != "document":
            raise ValueError("a constituent report is delivered whole")
        if any(row.trade_date != batch.document.document_date for row in self.data):
            raise ValueError("every row trade_date must equal document.document_date")
        declared = {index.code: index.constituent_count for index in batch.indexes}
        if len(declared) != len(batch.indexes):
            raise ValueError("every listed index must be declared once")
        _require_root_parents(batch.indexes)
        if len({(row.index_code, row.symbol) for row in self.data}) != len(self.data):
            raise PydanticCustomError(
                "duplicate_delivery_key",
                "duplicate (index_code, symbol) rows are not allowed",
            )
        listed: dict[str, int] = {}
        for row in self.data:
            listed[row.index_code] = listed.get(row.index_code, 0) + 1
        if listed != declared:
            raise ValueError("every index must list exactly its declared constituent_count rows")
        ensure_payload_size_within_limit(self.model_dump(mode="json"))
        return self


class IndexChangeRow(_IngressRow):
    """One effective addition or deletion, as the publisher's workbook lists it."""

    ordinal: int = Field(ge=1)
    effective_date: date
    action: Literal["add", "delete"]
    symbol: str = Field(pattern=EXCHANGE_SYMBOL_PATTERN)
    name_en: Optional[IndexText] = None
    name_zh: Optional[IndexText] = None
    listing_place: str = Field(min_length=1, max_length=50)
    # Constituent count once every change of that effective date is applied.
    constituents_after: NonNegativeInt
    # How many rows of this action the effective date lists, signed by the
    # action (``+2`` for two additions), when the publisher states it.
    count_change: Optional[int] = None

    @model_validator(mode="after")
    def count_change_signed_by_action(self) -> "IndexChangeRow":
        if self.count_change is not None and (
            self.count_change == 0 or (self.count_change > 0) != (self.action == "add")
        ):
            raise ValueError("count_change is positive for additions and negative for deletions")
        return self


class IndexChangesBatch(IndexDocumentBatch):
    index: IndexDescriptor
    # Earliest effective date the publisher lists for this index.
    history_start: date


class IndexChangesPayload(BaseModel):
    """Payload body for ``index_constituent_changes.v1``: one index's complete change history."""

    model_config = ConfigDict(extra="forbid")

    batch: IndexChangesBatch
    data: list[IndexChangeRow] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_history(self) -> "IndexChangesPayload":
        batch = self.batch
        _require_declared_rows(batch, self.data)
        if batch.document.kind != "change_workbook" or batch.document.part_key != batch.index.code:
            raise ValueError("a change workbook is delivered one index at a time")
        dates = [row.effective_date for row in self.data]
        if min(dates) != batch.history_start:
            raise ValueError("history_start must equal the earliest effective_date")
        if max(dates) > batch.document.document_date:
            raise ValueError("no effective_date may follow document.document_date")
        if len({row.ordinal for row in self.data}) != len(self.data):
            raise PydanticCustomError(
                "duplicate_delivery_key", "duplicate ordinal rows are not allowed"
            )
        if len({(row.effective_date, row.symbol, row.action) for row in self.data}) != len(
            self.data
        ):
            raise PydanticCustomError(
                "duplicate_delivery_key",
                "duplicate (effective_date, symbol, action) rows are not allowed",
            )
        by_date: dict[date, list[IndexChangeRow]] = {}
        for row in self.data:
            by_date.setdefault(row.effective_date, []).append(row)
        for rows in by_date.values():
            if len({row.constituents_after for row in rows}) != 1:
                raise ValueError(
                    "every row of an effective date states the same constituents_after"
                )
            for action in ("add", "delete"):
                listed = [row for row in rows if row.action == action]
                stated = {row.count_change for row in listed} - {None}
                signed = len(listed) if action == "add" else -len(listed)
                if stated and stated != {signed}:
                    raise ValueError("count_change must equal the rows of its action on that date")
        ensure_payload_size_within_limit(self.model_dump(mode="json"))
        return self


# --- Three major institutional investors (三大法人) --------------------------------
#
# TWSE and TPEx each publish two tables per trading day: market totals in TWD and
# per-security flows in shares. One request carries one whole table; its row is
# keyed by the board (``TWSE`` or ``TPEX``), the daily plan's only member.
INSTITUTIONAL_BOARDS = ("TWSE", "TPEX")
InstitutionalBoard = Literal["TWSE", "TPEX"]
# Leaf investor categories, the subtotals the exchanges print, and the total.
# The total excludes ``foreign_dealer``: the exchanges count it under dealers.
INSTITUTIONAL_LEAF_CATEGORIES = (
    "foreign_ex_dealer",
    "foreign_dealer",
    "investment_trust",
    "dealer_proprietary",
    "dealer_hedge",
)
INSTITUTIONAL_CATEGORIES = (
    *INSTITUTIONAL_LEAF_CATEGORIES,
    "foreign_total",
    "dealer_total",
    "total",
)
InstitutionalCategory = Literal[
    "foreign_ex_dealer",
    "foreign_dealer",
    "investment_trust",
    "dealer_proprietary",
    "dealer_hedge",
    "foreign_total",
    "dealer_total",
    "total",
]
# Signed integer amounts: TWD in the summary, shares per security.
InstitutionalAmount = Annotated[int, Field(ge=-(10**18), le=10**18)]
InstitutionalVolume = Annotated[int, Field(ge=0, le=10**18)]
# Securities of both boards: four- to six-character codes such as 2330, 00631L, 030001.
INSTITUTIONAL_SECURITY_PATTERN = r"^[0-9A-Z]{4,6}$"
INSTITUTIONAL_MAX_ROWS = 20_000
INSTITUTIONAL_MAX_ROWS_BYTES = 16 * 1024 * 1024


class InstitutionalFlowBatch(IngressBatch):
    """One exchange table of one trading day; the exchange's response is mandatory evidence."""

    delivery_mode: Literal["incremental", "backfill"]  # type: ignore[assignment]
    declared_record_count: Literal[1]  # type: ignore[assignment]
    source_raw_ref: str = Field(max_length=2048, pattern=RAW_OBJECT_REF_PATTERN)
    source_raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: None = None
    sequence_count: None = None


class _InstitutionalTableRow(_IngressRow):
    # The board is the plan member; ``source_symbol`` names the exchange's table.
    symbol: InstitutionalBoard
    source_symbol: Optional[str] = Field(default=None, min_length=1, max_length=100)
    trade_date: date
    currency: Literal["TWD"]


class InstitutionalFlowAmount(BaseModel):
    """One investor category's TWD totals as the exchange printed them."""

    model_config = ConfigDict(extra="forbid")

    category: InstitutionalCategory
    buy_amount: InstitutionalVolume
    sell_amount: InstitutionalVolume
    net_amount: InstitutionalAmount


class InstitutionalFlowSummaryRow(_InstitutionalTableRow):
    """A board's market totals of one day: every leaf category and the total."""

    categories: list[InstitutionalFlowAmount] = Field(min_length=6, max_length=8)

    @model_validator(mode="after")
    def validate_categories(self) -> "InstitutionalFlowSummaryRow":
        names = [item.category for item in self.categories]
        if len(names) != len(set(names)):
            raise ValueError("summary categories must be unique")
        if not {*INSTITUTIONAL_LEAF_CATEGORIES, "total"} <= set(names):
            raise ValueError("summary must carry every leaf category and the total")
        return self


class InstitutionalFlowStockRow(_InstitutionalTableRow):
    """A board's per-security flows of one day, described but not carried.

    The rows are a stored JSON object in the layout ``INSTITUTIONAL_STOCK_COLUMNS``;
    its size, checksum and row count are proven again before it is read.
    """

    row_count: int = Field(ge=1, le=INSTITUTIONAL_MAX_ROWS)
    rows_ref: str = Field(max_length=2048, pattern=RAW_OBJECT_REF_PATTERN)
    rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rows_bytes: int = Field(ge=1, le=INSTITUTIONAL_MAX_ROWS_BYTES)
    rows_format: Literal["json"]
    volume_unit: Literal["share"]


class InstitutionalStockColumn(NamedTuple):
    name: str
    type: Literal["string", "int64"]
    nullable: bool
    description: str


def _flow_columns(category: str, label: str, *, nullable: bool = False) -> list:
    return [
        InstitutionalStockColumn(f"{category}_{side}", "int64", nullable, f"{label} {text}")
        for side, text in (
            ("buy", "shares bought"),
            ("sell", "shares sold"),
            ("net", "net shares (buy - sell)"),
        )
    ]


# The rows object: ``{"columns": [names...], "rows": [[values...], ...]}``.
INSTITUTIONAL_STOCK_COLUMNS: tuple[InstitutionalStockColumn, ...] = (
    InstitutionalStockColumn("symbol", "string", False, "security code as the board lists it"),
    InstitutionalStockColumn("name", "string", True, "security name as the board lists it"),
    *_flow_columns("foreign_ex_dealer", "foreign and mainland investors excluding dealers:"),
    *_flow_columns("foreign_dealer", "foreign dealers:"),
    *_flow_columns("foreign_total", "foreign total (TPEx only):", nullable=True),
    *_flow_columns("investment_trust", "investment trusts:"),
    *_flow_columns("dealer_proprietary", "dealers trading on own account:"),
    *_flow_columns("dealer_hedge", "dealers hedging:"),
    InstitutionalStockColumn(
        "dealer_total_buy", "int64", True, "dealers total: shares bought (TPEx only)"
    ),
    InstitutionalStockColumn(
        "dealer_total_sell", "int64", True, "dealers total: shares sold (TPEx only)"
    ),
    InstitutionalStockColumn("dealer_total_net", "int64", False, "dealers total: net shares"),
    InstitutionalStockColumn(
        "total_net", "int64", False, "three institutional investors: net shares"
    ),
)


class InstitutionalFlowSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch: InstitutionalFlowBatch
    data: list[InstitutionalFlowSummaryRow] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def validate_single_table(self) -> "InstitutionalFlowSummaryPayload":
        _require_single_table_day(self.batch, self.data[0])
        return self


class InstitutionalFlowStockPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch: InstitutionalFlowBatch
    data: list[InstitutionalFlowStockRow] = Field(min_length=1, max_length=1)

    @model_validator(mode="after")
    def validate_single_table(self) -> "InstitutionalFlowStockPayload":
        _require_single_table_day(self.batch, self.data[0])
        return self


def _require_single_table_day(batch: InstitutionalFlowBatch, row: _InstitutionalTableRow) -> None:
    if row.trade_date != batch.data_date:
        raise ValueError("table trade_date must equal batch data_date")
    if batch.coverage_start_date not in (None, batch.data_date) or (
        batch.coverage_end_date not in (None, batch.data_date)
    ):
        raise ValueError("table coverage must be the single batch data_date")


class _IngressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dataset_key: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$")
    schema_id: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$")
    schema_version: int = Field(ge=1)
    source: str = Field(min_length=1, max_length=50, pattern=SOURCE_NAME_PATTERN)
    request_key: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=100)
    fetched_at: datetime
    delivery: Optional[IngressDeliveryMetadata] = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("fetched_at")
    @classmethod
    def require_utc_aware_fetched_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fetched_at must include a timezone")
        return value.astimezone(timezone.utc)


class MarketEODIngressRequest(_IngressRequest):
    """Complete ``market_eod.v1`` request."""

    schema_id: Literal["market_eod"]
    schema_version: Literal[1]
    payload: MarketEODPayload


class MarketEODIngressRequestV2(_IngressRequest):
    """Complete ``market_eod.v2`` request."""

    schema_id: Literal["market_eod"]
    schema_version: Literal[2]
    payload: MarketEODPayloadV2


class MarketMinuteIngressRequest(_IngressRequest):
    """Complete ``market_minute.v1`` request shape with runtime normalization."""

    schema_id: Literal["market_minute"]
    schema_version: Literal[1]
    payload: MarketMinutePayload

    @model_validator(mode="after")
    def require_shioaji_usage_snapshots(self) -> "MarketMinuteIngressRequest":
        batch = self.payload.batch
        digest = minute_sequence_key_digest(
            dataset_key=self.dataset_key,
            data_date=batch.data_date,
            snapshot_id=batch.snapshot_id,
            sequence=batch.sequence,
        )
        if self.request_key != f"mmr:{digest}":
            raise ValueError("request_key must equal the canonical minute request identity")
        if self.idempotency_key != f"mms:{digest}":
            raise ValueError("idempotency_key must equal the canonical minute sequence identity")
        if self.source == "shioaji" and self.payload.batch.provider_usage_before is None:
            raise ValueError("shioaji minute deliveries require provider usage snapshots")
        return self


class FuturesEODIngressRequest(_IngressRequest):
    """Complete ``futures_eod.v1`` contract request."""

    schema_id: Literal["futures_eod"]
    schema_version: Literal[1]
    source: Literal["taifex"]
    payload: FuturesEODPayload


class IntradayPartitionIngressRequest(_IngressRequest):
    """Complete ``intraday_partition.v1`` request."""

    schema_id: Literal["intraday_partition"]
    schema_version: Literal[1]
    payload: IntradayPartitionPayload


class ExchangeDailyQuoteIngressRequest(_IngressRequest):
    """Complete ``exchange_daily_quote.v1`` request."""

    schema_id: Literal["exchange_daily_quote"]
    schema_version: Literal[1]
    payload: ExchangeDailyQuotePayload


class IndexConstituentsIngressRequest(_IngressRequest):
    """Complete ``index_constituents.v1`` request."""

    schema_id: Literal["index_constituents"]
    schema_version: Literal[1]
    payload: IndexConstituentsPayload


class IndexChangesIngressRequest(_IngressRequest):
    """Complete ``index_constituent_changes.v1`` request."""

    schema_id: Literal["index_constituent_changes"]
    schema_version: Literal[1]
    payload: IndexChangesPayload


class InstitutionalFlowSummaryIngressRequest(_IngressRequest):
    """Complete ``institutional_flow_summary.v1`` request."""

    schema_id: Literal["institutional_flow_summary"]
    schema_version: Literal[1]
    source: Literal["twse", "tpex"]
    payload: InstitutionalFlowSummaryPayload

    @model_validator(mode="after")
    def board_matches_source(self) -> "InstitutionalFlowSummaryIngressRequest":
        _require_board_source(self.source, self.payload.data[0].symbol)
        return self


class InstitutionalFlowStockIngressRequest(_IngressRequest):
    """Complete ``institutional_flow_stock.v1`` request."""

    schema_id: Literal["institutional_flow_stock"]
    schema_version: Literal[1]
    source: Literal["twse", "tpex"]
    payload: InstitutionalFlowStockPayload

    @model_validator(mode="after")
    def board_matches_source(self) -> "InstitutionalFlowStockIngressRequest":
        _require_board_source(self.source, self.payload.data[0].symbol)
        return self


def _require_board_source(source: str, board: str) -> None:
    # Each exchange publishes only its own board.
    if board != source.upper():
        raise ValueError("table board must be the source exchange")


INSTITUTIONAL_FLOW_REQUESTS = (
    InstitutionalFlowSummaryIngressRequest,
    InstitutionalFlowStockIngressRequest,
)
INSTITUTIONAL_FLOW_SCHEMAS = frozenset({"institutional_flow_summary", "institutional_flow_stock"})


# Requests that carry a publisher document rather than market observations.
INDEX_REFERENCE_REQUESTS = (
    IndexConstituentsIngressRequest,
    IndexChangesIngressRequest,
)


def minute_sequence_key_digest(
    *, dataset_key: str, data_date: date, snapshot_id: str, sequence: int
) -> str:
    """Return SHA-256 of the canonical minute sequence identity object."""
    canonical_identity = {
        "data_date": data_date.isoformat(),
        "dataset_key": dataset_key,
        "sequence": sequence,
        "snapshot_id": snapshot_id,
    }
    return hashlib.sha256(
        json.dumps(
            canonical_identity,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


IngressRequestV1 = (
    MarketEODIngressRequest
    | MarketMinuteIngressRequest
    | FuturesEODIngressRequest
    | IntradayPartitionIngressRequest
    | ExchangeDailyQuoteIngressRequest
    | IndexConstituentsIngressRequest
    | IndexChangesIngressRequest
    | InstitutionalFlowSummaryIngressRequest
    | InstitutionalFlowStockIngressRequest
)
IngressRequest = IngressRequestV1 | MarketEODIngressRequestV2
