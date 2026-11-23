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
    "futures_first_date",
    "futures_latest_date",
]


class EODCoverageResponse(BaseModel):
    first_date: date
    latest_date: date
    latest_close: Decimal | None = None


class MinuteCoverageResponse(BaseModel):
    first_bar_at: datetime
    latest_bar_at: datetime
    latest_close: Decimal


class IntradayCoverageResponse(BaseModel):
    """Exchange-local trade dates of the first and latest stored day."""

    first_date: date
    latest_date: date


class InstrumentCoverageResponse(BaseModel):
    eod: EODCoverageResponse | None = None
    minute: MinuteCoverageResponse | None = None
    futures: EODCoverageResponse | None = None
    intraday_bars: IntradayCoverageResponse | None = None
    intraday_ticks: IntradayCoverageResponse | None = None


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
    frequency: Literal["daily", "minute", "tick"]
    data_kind: Literal["eod", "minute", "futures_eod", "intraday_bars", "intraday_ticks"]
    interval: Literal["1d", "1m", "tick"]
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
    currency: str | None = None
    trade_date: date
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    total_ticks: int | None = None
    turnover: Decimal | None = None
    source: str | None = None
    # Fields whose value is not ``source``'s: ``{"close": "hkex"}`` after the
    # exchange's official quote replaced it, ``"manual"`` after an Admin
    # correction.  Null when every value is the row source's own.
    field_sources: dict[str, str] | None = None
    source_fetched_at: datetime | None = None
    asof_ts: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EODListResponse(BaseModel):
    success: Literal[True] = True
    data: list[EODResponse]
    pagination: CursorPaginationInfo


class FuturesEODResponse(BaseModel):
    contract_id: UUID
    instrument_id: UUID
    product_code: str
    contract_code: str
    contract_month: str | None
    trade_date: date
    session: Literal["regular", "after_hours"]
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    settlement_price: Decimal | None = None
    open_interest: int | None = None
    source: str | None = None
    source_fetched_at: datetime | None = None
    asof_ts: datetime
    created_at: datetime
    updated_at: datetime


class FuturesEODListResponse(BaseModel):
    success: Literal[True] = True
    data: list[FuturesEODResponse]
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


IntradayInterval = Literal["1m", "5m", "15m", "30m", "60m"]


class IntradayInstrumentResponse(BaseModel):
    instrument_id: UUID
    symbol: str
    name: str | None = None
    market: str
    asset_class: str
    currency: str | None = None


class IntradayUnitsResponse(BaseModel):
    """How to read the rows; volumes are lots, unlike the share-based EOD."""

    volume: Literal["lot"] = "lot"
    price_adjustment: Literal["none"] = "none"
    instants: Literal["UTC"] = "UTC"
    trade_date: Literal["exchange_local"] = "exchange_local"
    market_timezone: str


class IntradayBarResponse(BaseModel):
    trade_date: date
    bar_start_time: datetime
    bar_end_time: datetime
    session: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trade_count: int | None = None
    vwap: Decimal | None = None
    source: str
    source_fetched_at: datetime


class IntradayBarListResponse(BaseModel):
    success: Literal[True] = True
    instrument: IntradayInstrumentResponse
    dataset_key: str
    interval: IntradayInterval
    units: IntradayUnitsResponse
    data: list[IntradayBarResponse]
    pagination: CursorPaginationInfo
    range: ResolvedDateRangeResponse


class IntradayTickResponse(BaseModel):
    trade_date: date
    trade_time: datetime
    sequence: int
    session: str
    price: Decimal
    volume: int
    tick_type: int | None = None
    bid_price: Decimal | None = None
    bid_volume: int | None = None
    ask_price: Decimal | None = None
    ask_volume: int | None = None
    source: str
    source_fetched_at: datetime


class IntradayTickListResponse(BaseModel):
    success: Literal[True] = True
    instrument: IntradayInstrumentResponse
    dataset_key: str
    units: IntradayUnitsResponse
    trade_date: date | None = None
    anchor: Literal["explicit", "latest_available"]
    data: list[IntradayTickResponse]
    pagination: CursorPaginationInfo


class IntradayDownloadColumn(BaseModel):
    name: str
    type: str
    nullable: bool
    description: str


class IntradayDownloadLayout(BaseModel):
    """The columns every object in the manifest holds, as the contract publishes them."""

    schema_id: Literal["intraday_partition"] = "intraday_partition"
    schema_version: int
    record_kind: Literal["bar_1m", "tick"]
    format: Literal["parquet"] = "parquet"
    columns: list[IntradayDownloadColumn]


class IntradayDownloadDay(BaseModel):
    trade_date: date
    row_count: int


class IntradayDownloadObject(BaseModel):
    """One object to fetch, and the only trade dates to take from it.

    A month object can still hold rows of a day republished after the merge;
    those rows are superseded and that day is listed under its day object.
    """

    url: str
    object_layout: Literal["day", "month"]
    object_etag: str
    trade_dates: list[IntradayDownloadDay]


class IntradayDownloadQuota(BaseModel):
    usage_date: date
    daily_limit: int
    used: int
    remaining: int


class IntradayDownloadManifestResponse(BaseModel):
    success: Literal[True] = True
    instrument: IntradayInstrumentResponse
    dataset_key: str
    month: str
    units: IntradayUnitsResponse
    layout: IntradayDownloadLayout
    expires_at: datetime
    objects: list[IntradayDownloadObject]
    quota: IntradayDownloadQuota


class CalendarResponse(BaseModel):
    market: str
    trade_date: date
    is_open: bool
    session_open: str | None = None
    session_close: str | None = None
    holiday_name: str | None = None
    day_status: Literal["open", "closed", "settlement_only"] = "open"
    description: str | None = None
    calendar_source: Literal["published", "weekday_default"] = "published"
    calendar_revision: int | None = None

    model_config = ConfigDict(from_attributes=True)


class CalendarListResponse(BaseModel):
    success: Literal[True] = True
    data: list[CalendarResponse]
    pagination: PaginationInfo


class PublishedCalendarYearResponse(BaseModel):
    market: str
    year: int
    revision: int | None
    status: Literal["published", "defaulted", "mixed"]
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
