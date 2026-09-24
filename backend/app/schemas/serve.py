"""Typed response contracts for the canonical read-only Serve API."""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import PaginationInfo
from app.services.slot_identity import CanonicalSlotId

SortDirection = Literal["asc", "desc"]
InstrumentSort = Literal[
    "market",
    "symbol",
    "name",
    "asset_class",
    "currency",
    "status",
    "eod_first_date",
    "eod_latest_date",
    "eod_latest_close",
    "minute_first_bar_at",
    "minute_latest_bar_at",
    "minute_latest_close",
]


class EODCoverageResponse(BaseModel):
    first_date: date
    latest_date: date
    latest_close: Decimal | None = None


class MinuteCoverageResponse(BaseModel):
    first_bar_at: datetime
    latest_bar_at: datetime
    latest_close: Decimal


class InstrumentCoverageResponse(BaseModel):
    eod: EODCoverageResponse | None = None
    minute: MinuteCoverageResponse | None = None


class InstrumentResponse(BaseModel):
    instrument_id: UUID
    asset_class: str
    market: str
    symbol: str
    name: str | None = None
    currency: str | None = None
    timezone: str | None = None
    status: str
    listed_date: date | None = None
    delisted_date: date | None = None
    coverage: InstrumentCoverageResponse


class InstrumentFacetsResponse(BaseModel):
    markets: list[str]
    asset_classes: list[str]
    statuses: list[str]


class InstrumentListResponse(BaseModel):
    success: Literal[True] = True
    data: list[InstrumentResponse]
    pagination: PaginationInfo
    facets: InstrumentFacetsResponse


class InstrumentDetailResponse(BaseModel):
    success: Literal[True] = True
    data: InstrumentResponse


class DatasetCoverageResponse(BaseModel):
    coverage_start_date: date | None = None
    coverage_end_date: date | None = None
    instrument_count: int


class DatasetResponse(BaseModel):
    dataset_key: str
    display_name: str
    market: str
    asset_class: str
    frequency: Literal["daily", "minute"]
    data_kind: Literal["eod", "minute"]
    interval: Literal["1d", "1m"]
    availability: Literal["available", "configured_empty"]
    coverage: DatasetCoverageResponse


class DatasetListResponse(BaseModel):
    success: Literal[True] = True
    data: list[DatasetResponse]


class CursorPaginationInfo(BaseModel):
    page_size: int
    next_cursor: str | None = None


class EODResponse(BaseModel):
    instrument_id: UUID
    symbol: str
    name: str | None = None
    market: str
    trade_date: date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    total_ticks: int | None = None
    turnover: Decimal | None = None
    source: str | None = None
    source_fetched_at: datetime | None = None
    asof_ts: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EODListResponse(BaseModel):
    success: Literal[True] = True
    data: list[EODResponse]
    pagination: CursorPaginationInfo


class MinuteResponse(BaseModel):
    instrument_id: UUID
    symbol: str
    name: str | None = None
    market: str
    trade_date: date
    bar_start_time: datetime
    bar_end_time: datetime
    signal_time: datetime
    market_timezone: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None = None
    turnover: Decimal | None = None
    trade_count: int | None = None
    price_adjustment: str
    source: str
    source_fetched_at: datetime
    asof_ts: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ResolvedDateRangeResponse(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    anchor: Literal["explicit", "latest_available"]


class MinuteListResponse(BaseModel):
    success: Literal[True] = True
    data: list[MinuteResponse]
    pagination: CursorPaginationInfo
    range: ResolvedDateRangeResponse


class CalendarResponse(BaseModel):
    market: str
    trade_date: date
    is_open: bool
    session_open: str | None = None
    session_close: str | None = None
    holiday_name: str | None = None
    day_status: Literal["open", "closed", "settlement_only"] = "open"
    description: str | None = None

    model_config = ConfigDict(from_attributes=True)


class CalendarListResponse(BaseModel):
    success: Literal[True] = True
    data: list[CalendarResponse]
    pagination: PaginationInfo


class PublishedCalendarYearResponse(BaseModel):
    market: str
    year: int
    revision: int
    status: Literal["published"]
    coverage_complete: Literal[True]
    timezone: str
    expected_days: int
    actual_days: int
    days: list[CalendarResponse]


class PublishedCalendarYearEnvelope(BaseModel):
    success: Literal[True] = True
    data: PublishedCalendarYearResponse


class MarketFreshnessSummaryResponse(BaseModel):
    market: str
    slot_id: CanonicalSlotId
    scheduled_local_time: time
    timezone: str
    status: Literal["not_due", "fresh", "partial", "late", "failed", "never_received"]
    expected_data_date: date | None = None
    coverage_data_date: date | None = None
    last_successful_update_at: datetime | None = None
    last_complete_at: datetime | None = None
    next_scheduled_at: datetime
    feed_count: int
    fresh_feed_count: int
    late_feed_count: int


class MarketFreshnessSummaryListResponse(BaseModel):
    success: Literal[True] = True
    data: list[MarketFreshnessSummaryResponse]
