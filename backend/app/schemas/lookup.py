"""Schemas for the dedicated public lookup endpoints."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.common import PaginationInfo

InstrumentLookupSort = Literal[
    "market",
    "symbol",
    "name",
    "asset_class",
    "currency",
    "first_trade_date",
    "latest_trade_date",
    "latest_price",
    "status",
]
MacroSeriesLookupSort = Literal[
    "market",
    "source_code",
    "name",
    "frequency",
    "unit",
    "source",
]
SortDirection = Literal["asc", "desc"]


class InstrumentLookupItem(BaseModel):
    """Instrument fields exposed to the lookup UI."""

    instrument_id: UUID
    market: str
    symbol: str
    name: str | None
    asset_class: str
    currency: str | None
    status: str
    first_trade_date: date | None
    latest_trade_date: date | None
    latest_price: Decimal | None


class InstrumentLookupFacets(BaseModel):
    """Global instrument filter options."""

    markets: list[str]
    asset_classes: list[str]
    statuses: list[str]


class InstrumentLookupResponse(BaseModel):
    """Paginated instrument lookup response."""

    success: bool = True
    data: list[InstrumentLookupItem]
    pagination: PaginationInfo
    facets: InstrumentLookupFacets


class MacroSeriesLookupItem(BaseModel):
    """Macro series fields exposed to the lookup UI."""

    series_id: UUID
    market: str | None
    source_code: str | None
    name: str
    frequency: str | None
    unit: str | None
    source: str | None


class MacroSeriesLookupFacets(BaseModel):
    """Global macro series filter options."""

    markets: list[str]
    frequencies: list[str]
    sources: list[str]


class MacroSeriesLookupResponse(BaseModel):
    """Paginated macro series lookup response."""

    success: bool = True
    data: list[MacroSeriesLookupItem]
    pagination: PaginationInfo
    facets: MacroSeriesLookupFacets
