"""
Shared type definitions for normalization.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID


@dataclass
class MappedRecord:
    """A normalized/mapped record ready for canonical storage."""

    symbol: str
    trade_date: datetime
    market: Optional[str] = None
    asset_class: Optional[str] = None
    name: Optional[str] = None
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Optional[Decimal] = None
    volume: Optional[int] = None
    turnover: Optional[Decimal] = None
    up_volume: Optional[int] = None
    down_volume: Optional[int] = None
    up_ticks: Optional[int] = None
    down_ticks: Optional[int] = None
    total_ticks: Optional[int] = None
    source: Optional[str] = None
    raw_data: Optional[dict] = None
    identifier_type: Optional[str] = None
    identifier_value: Optional[str] = None


@dataclass
class CorporateActionRecord:
    """A normalized corporate action record."""

    symbol: str
    action_type: Optional[str] = None
    ex_date: Optional[date] = None
    record_date: Optional[date] = None
    pay_date: Optional[date] = None
    ratio: Optional[Decimal] = None
    cash_amount: Optional[Decimal] = None
    currency: Optional[str] = None
    source: Optional[str] = None
    name: Optional[str] = None
    raw_data: Optional[dict] = None
    extra: Optional[dict] = None
    identifier_type: Optional[str] = None
    identifier_value: Optional[str] = None


@dataclass
class MacroObservationRecord:
    """A normalized macro observation record with series metadata."""

    source_code: str
    obs_date: Optional[date]
    value: Optional[Decimal] = None
    name: Optional[str] = None
    unit: Optional[str] = None
    frequency: Optional[str] = None
    market: Optional[str] = None
    source: Optional[str] = None
    raw_data: Optional[dict] = None


@dataclass
class FuturesContractRecord:
    """A normalized futures contract record."""

    symbol: str
    contract_code: Optional[str] = None
    contract_month: Optional[str] = None
    expiry_date: Optional[date] = None
    currency: Optional[str] = None
    source: Optional[str] = None
    name: Optional[str] = None
    raw_data: Optional[dict] = None
    extra: Optional[dict] = None
    identifier_type: Optional[str] = None
    identifier_value: Optional[str] = None


@dataclass
class FuturesContinuousRecord:
    """A normalized continuous futures EOD record."""

    symbol: str
    trade_date: datetime
    name: Optional[str] = None
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Optional[Decimal] = None
    volume: Optional[int] = None
    turnover: Optional[Decimal] = None
    source: Optional[str] = None
    raw_data: Optional[dict] = None
    identifier_type: Optional[str] = None
    identifier_value: Optional[str] = None
    roll_rule_id: Optional[UUID] = None
    roll_rule_name: Optional[str] = None
    roll_rule_description: Optional[str] = None
    roll_rule_config: Optional[dict] = None
