"""Provider-neutral, versioned ingress contracts."""

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.schemas.payload_limits import (
    ensure_data_items_count_within_limit,
    ensure_payload_size_within_limit,
)

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


class _IngressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    dataset_key: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$")
    schema_id: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$")
    schema_version: int = Field(ge=1)
    source: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$")
    request_key: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=100)
    fetched_at: datetime

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


IngressRequestV1 = MarketEODIngressRequest | FuturesContinuousEODIngressRequest
