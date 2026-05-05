"""
Serve API Pydantic schemas.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import PaginatedResponse


class InstrumentResponse(BaseModel):
    """Single instrument response."""

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
    latest_trade_date: Optional[date] = None
    latest_price: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)


class InstrumentListResponse(PaginatedResponse[InstrumentResponse]):
    """Paginated instrument list response."""

    pass


class EODResponse(BaseModel):
    """Single EOD data response."""

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
    up_volume: Optional[int] = None
    down_volume: Optional[int] = None
    up_ticks: Optional[int] = None
    down_ticks: Optional[int] = None
    total_ticks: Optional[int] = None
    turnover: Optional[Decimal] = None
    source: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class EODListResponse(PaginatedResponse[EODResponse]):
    """Paginated EOD data response."""

    pass


class CorporateActionResponse(BaseModel):
    """Single corporate action response."""

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
    """Paginated corporate action response."""

    pass


class MacroSeriesResponse(BaseModel):
    """Single macro series response."""

    series_id: UUID
    name: str
    unit: Optional[str] = None
    frequency: Optional[str] = None
    market: Optional[str] = None
    source_code: Optional[str] = None
    source: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class MacroSeriesListResponse(PaginatedResponse[MacroSeriesResponse]):
    """Paginated macro series response."""

    pass


class MacroObservationResponse(BaseModel):
    """Single macro observation response."""

    id: UUID
    series_id: UUID
    series_name: Optional[str] = None
    obs_date: date
    value: Optional[Decimal] = None
    source: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class MacroObservationListResponse(PaginatedResponse[MacroObservationResponse]):
    """Paginated macro observation response."""

    pass


class FuturesContractResponse(BaseModel):
    """Single futures contract response."""

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
    """Paginated futures contract response."""

    pass


class FuturesContinuousResponse(BaseModel):
    """Single continuous futures EOD response."""

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
    source: Optional[str] = None
    roll_rule_id: Optional[UUID] = None
    roll_rule_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class FuturesContinuousListResponse(PaginatedResponse[FuturesContinuousResponse]):
    """Paginated continuous futures response."""

    pass


class CalendarResponse(BaseModel):
    """Trading calendar response."""

    market: str
    trade_date: date
    is_open: bool
    session_open: Optional[str] = None
    session_close: Optional[str] = None
    holiday_name: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class CalendarListResponse(PaginatedResponse[CalendarResponse]):
    """Paginated calendar response."""

    pass
