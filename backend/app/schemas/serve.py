"""Serve API 使用的 Pydantic schema。"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import PaginatedResponse


class InstrumentResponse(BaseModel):
    """單一商品回應。"""

    instrument_id: UUID
    asset_class: str
    market: str
    symbol: str
    name: Optional[str] = None
    currency: Optional[str] = None
    timezone: Optional[str] = None
    status: str
    listed_date: Optional[date] = None
    delisted_date: Optional[date] = None
    first_trade_date: Optional[date] = None
    latest_trade_date: Optional[date] = None
    latest_price: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)


class InstrumentListResponse(PaginatedResponse[InstrumentResponse]):
    """分頁商品列表回應。"""

    pass


class EODResponse(BaseModel):
    """單一日線資料回應。"""

    instrument_id: UUID
    symbol: str
    name: Optional[str] = None
    market: str
    trade_date: date
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Optional[Decimal] = None
    volume: Optional[int] = None
    total_ticks: Optional[int] = None
    turnover: Optional[Decimal] = None
    source: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class EODListResponse(PaginatedResponse[EODResponse]):
    """分頁日線資料回應。"""

    pass


class CorporateActionResponse(BaseModel):
    """單一公司行動回應。"""

    action_id: UUID
    instrument_id: UUID
    symbol: str
    name: Optional[str] = None
    market: str
    action_type: str
    ex_date: Optional[date] = None
    record_date: Optional[date] = None
    pay_date: Optional[date] = None
    ratio: Optional[Decimal] = None
    cash_amount: Optional[Decimal] = None
    currency: Optional[str] = None
    source: Optional[str] = None
    extra: Optional[dict] = None

    model_config = ConfigDict(from_attributes=True)


class CorporateActionListResponse(PaginatedResponse[CorporateActionResponse]):
    """分頁公司行動回應。"""

    pass


class MacroSeriesResponse(BaseModel):
    """單一總經時間序列回應。"""

    series_id: UUID
    name: str
    unit: Optional[str] = None
    frequency: Optional[str] = None
    market: Optional[str] = None
    source_code: Optional[str] = None
    source: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class MacroSeriesListResponse(PaginatedResponse[MacroSeriesResponse]):
    """分頁總經時間序列回應。"""

    pass


class MacroObservationResponse(BaseModel):
    """單一總經觀測值回應。"""

    id: UUID
    series_id: UUID
    series_name: Optional[str] = None
    obs_date: date
    value: Optional[Decimal] = None
    source: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class MacroObservationListResponse(PaginatedResponse[MacroObservationResponse]):
    """分頁總經觀測值回應。"""

    pass


class FuturesContractResponse(BaseModel):
    """單一期貨合約回應。"""

    contract_id: UUID
    instrument_id: UUID
    symbol: str
    name: Optional[str] = None
    contract_code: str
    contract_month: Optional[str] = None
    expiry_date: Optional[date] = None
    currency: Optional[str] = None
    source: Optional[str] = None
    extra: Optional[dict] = None

    model_config = ConfigDict(from_attributes=True)


class FuturesContractListResponse(PaginatedResponse[FuturesContractResponse]):
    """分頁期貨合約回應。"""

    pass


class FuturesContinuousResponse(BaseModel):
    """單一連續期貨日線回應。"""

    id: UUID
    instrument_id: UUID
    symbol: str
    name: Optional[str] = None
    trade_date: date
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Optional[Decimal] = None
    volume: Optional[int] = None
    turnover: Optional[Decimal] = None
    open_interest: Optional[int] = None
    active_contract_code: Optional[str] = None
    roll_adjustment: Optional[Decimal] = None
    source: Optional[str] = None
    roll_rule_id: Optional[UUID] = None
    roll_rule_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class FuturesContinuousListResponse(PaginatedResponse[FuturesContinuousResponse]):
    """分頁連續期貨回應。"""

    pass


class BondResponse(BaseModel):
    """單一債券商品回應。"""

    instrument_id: UUID
    symbol: str
    name: Optional[str] = None
    market: str
    currency: Optional[str] = None
    issuer: Optional[str] = None
    coupon: Optional[Decimal] = None
    maturity_date: Optional[date] = None
    rating: Optional[str] = None
    face_value: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)


class BondListResponse(PaginatedResponse[BondResponse]):
    """分頁債券商品回應。"""

    pass


class BondEODResponse(BaseModel):
    """單一債券日資料回應。"""

    id: UUID
    instrument_id: UUID
    symbol: str
    name: Optional[str] = None
    market: str
    trade_date: date
    yield_to_maturity: Optional[Decimal] = None
    clean_price: Optional[Decimal] = None
    dirty_price: Optional[Decimal] = None
    duration: Optional[Decimal] = None
    source: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class BondEODListResponse(PaginatedResponse[BondEODResponse]):
    """分頁債券日資料回應。"""

    pass


class CalendarResponse(BaseModel):
    """交易日曆回應。"""

    market: str
    trade_date: date
    is_open: bool
    session_open: Optional[str] = None
    session_close: Optional[str] = None
    holiday_name: Optional[str] = None
    day_status: Literal["open", "closed", "settlement_only"] = "open"
    description: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class CalendarListResponse(PaginatedResponse[CalendarResponse]):
    """分頁交易日曆回應。"""

    pass


class PublishedCalendarYearResponse(BaseModel):
    """Fail-closed scheduler contract: available only for complete published years."""

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
    """Public, provider-free projection of configured market delivery health."""

    market: str
    slot_id: str
    scheduled_local_time: time
    timezone: str
    status: Literal["not_due", "fresh", "partial", "late", "failed", "never_received"]
    expected_data_date: Optional[date] = None
    coverage_data_date: Optional[date] = None
    last_successful_update_at: Optional[datetime] = None
    last_complete_at: Optional[datetime] = None
    next_scheduled_at: datetime
    feed_count: int
    fresh_feed_count: int
    late_feed_count: int


class MarketFreshnessSummaryListResponse(BaseModel):
    success: bool = True
    data: list[MarketFreshnessSummaryResponse]
