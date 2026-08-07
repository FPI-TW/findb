"""Provider-neutral, versioned ingress contracts."""

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.schemas.payload_limits import (
    ensure_data_items_count_within_limit,
    ensure_payload_size_within_limit,
)
from app.services.slot_identity import normalize_slot_id
from app.vocabulary import SOURCE_NAME_PATTERN

NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
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

    @field_validator("slot_id", mode="before")
    @classmethod
    def normalize_legacy_slot_id(cls, value: object) -> str:
        # Compatibility is intentionally limited to this input boundary.  The
        # resulting model and generated contract schema expose canonical IDs.
        return normalize_slot_id(value)

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


class FuturesContinuousEODRow(_OHLCRow):
    """Provider-neutral continuous-futures daily row."""

    symbol: str = Field(min_length=1, max_length=50)
    source_symbol: Optional[str] = Field(default=None, min_length=1, max_length=100)
    trade_date: date
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    volume: Optional[NonNegativeInt] = None
    turnover: Optional[NonNegativeDecimal] = None
    open_interest: Optional[NonNegativeInt] = None
    active_contract_code: Optional[str] = Field(default=None, min_length=1, max_length=50)
    roll_rule: str = Field(min_length=1, max_length=50)
    roll_adjustment: Optional[Decimal] = None


def _validate_payload_batch(
    batch: IngressBatch,
    data: list[MarketEODRow] | list[FuturesContinuousEODRow],
) -> None:
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


class FuturesContinuousEODPayload(BaseModel):
    """Payload body for ``futures_continuous_eod.v1``."""

    model_config = ConfigDict(extra="forbid")

    batch: IngressBatch
    data: list[FuturesContinuousEODRow]

    @model_validator(mode="after")
    def validate_batch_and_rows(self) -> "FuturesContinuousEODPayload":
        _validate_payload_batch(self.batch, self.data)
        natural_keys = [(row.symbol, row.trade_date) for row in self.data]
        if len(natural_keys) != len(set(natural_keys)):
            raise PydanticCustomError(
                "duplicate_delivery_key",
                "duplicate (symbol, trade_date) rows are not allowed",
            )
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
        from zoneinfo import ZoneInfo

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


class FuturesContinuousEODIngressRequest(_IngressRequest):
    """Complete ``futures_continuous_eod.v1`` request."""

    schema_id: Literal["futures_continuous_eod"]
    schema_version: Literal[1]
    payload: FuturesContinuousEODPayload


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
    MarketEODIngressRequest | FuturesContinuousEODIngressRequest | MarketMinuteIngressRequest
)
