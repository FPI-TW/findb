"""
Admin API Pydantic schemas.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PaginatedResponse


# ── EOD Correction ────────────────────────────────────────────────────────────


class PatchEODRequest(BaseModel):
    """
    PATCH body for MarketDataEOD correction.
    Only explicitly provided OHLCV fields are updated.
    correction_reason is always required.
    """

    correction_reason: str = Field(..., min_length=1, max_length=2000)
    open: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    close: Optional[Decimal] = None
    volume: Optional[int] = None
    turnover: Optional[Decimal] = None


class PatchEODResponse(BaseModel):
    """Response after patching an EOD record."""

    success: bool = True
    correction_id: UUID
    record_id: UUID
    instrument_id: UUID
    trade_date: date
    message: str

    model_config = ConfigDict(from_attributes=True)


# ── DQ Issue Resolution ────────────────────────────────────────────────────────


class ResolveDQIssueRequest(BaseModel):
    """PATCH body for resolving a DQ issue."""

    correction_reason: str = Field(..., min_length=1, max_length=2000)


class ResolveDQIssueResponse(BaseModel):
    """Response after resolving a DQ issue."""

    success: bool = True
    correction_id: UUID
    issue_id: UUID
    resolved_at: datetime
    message: str

    model_config = ConfigDict(from_attributes=True)


# ── Correction List ────────────────────────────────────────────────────────────


class CorrectionResponse(BaseModel):
    """Single correction audit record."""

    id: UUID
    table_name: str
    record_id: UUID
    instrument_id: Optional[UUID] = None
    trade_date: Optional[date] = None
    corrected_by: str
    correction_reason: str
    before_snapshot: Optional[dict[str, Any]] = None
    after_snapshot: Optional[dict[str, Any]] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CorrectionListResponse(PaginatedResponse[CorrectionResponse]):
    """Paginated correction list."""

    pass
