"""
Shared type definitions for normalization.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Protocol


@dataclass
class MappedRecord:
    """A normalized/mapped record ready for canonical storage."""

    symbol: str
    trade_date: datetime
    market: Optional[str] = None
    asset_class: Optional[str] = None
    name: Optional[str] = None
    currency: Optional[str] = None
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Optional[Decimal] = None
    volume: Optional[int] = None
    turnover: Optional[Decimal] = None
    total_ticks: Optional[int] = None
    source: Optional[str] = None
    raw_data: Optional[dict] = None
    identifier_type: Optional[str] = None
    identifier_value: Optional[str] = None


@dataclass
class MarketMinuteRecord:
    """A provider-neutral canonical one-minute bar."""

    symbol: str
    trade_date: date
    bar_start_time: Optional[datetime]
    bar_end_time: Optional[datetime]
    signal_time: Optional[datetime]
    market_timezone: Optional[str]
    price_adjustment: Optional[str]
    open: Optional[Decimal]
    high: Optional[Decimal]
    low: Optional[Decimal]
    close: Optional[Decimal]
    volume: Optional[int] = None
    turnover: Optional[Decimal] = None
    trade_count: Optional[int] = None
    market: Optional[str] = None
    asset_class: Optional[str] = None
    name: Optional[str] = None
    currency: Optional[str] = None
    source: Optional[str] = None
    raw_data: Optional[dict] = None
    identifier_type: Optional[str] = None
    identifier_value: Optional[str] = None


class InstrumentResolvableRecord(Protocol):
    """Record shape that can resolve or create an instrument."""

    symbol: str
    name: Optional[str]
    identifier_type: Optional[str]
    identifier_value: Optional[str]


class EODLikeRecord(InstrumentResolvableRecord, Protocol):
    """Record shape used by EOD data-quality checks."""

    trade_date: datetime
    open: Optional[Decimal]
    high: Optional[Decimal]
    low: Optional[Decimal]
    close: Optional[Decimal]
    volume: Optional[int]
    raw_data: Optional[dict]
