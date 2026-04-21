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


class DQIssueResponse(BaseModel):
    """Single DQ issue record."""

    id: UUID
    run_id: Optional[UUID] = None
    instrument_id: Optional[UUID] = None
    trade_date: Optional[date] = None
    issue_type: str
    severity: str
    description: Optional[str] = None
    raw_data: Optional[dict] = None
    resolved: bool
    resolved_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DQIssueListResponse(PaginatedResponse[DQIssueResponse]):
    """Paginated DQ issue list."""

    pass


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


# ── Raw Payload ────────────────────────────────────────────────────────────────


class RawPayloadResponse(BaseModel):
    """Single raw market payload record."""

    idempotency_key: str
    run_id: UUID
    dataset_key: str
    source: str
    request_key: str
    payload: Any
    fetched_at: datetime
    expire_at: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RawPayloadListResponse(PaginatedResponse[RawPayloadResponse]):
    """Paginated raw payload list."""

    pass


class BulkRerunResponse(BaseModel):
    """Response for bulk rerun operation."""

    queued: int
    skipped: int
    errors: int
    new_run_ids: list[str]
    error_details: list[str]


# ── Instrument Cache ──────────────────────────────────────────────────────────


class InstrumentCacheItem(BaseModel):
    """Single item in the generated static instrument cache."""

    instrument_id: str = Field(..., min_length=1)
    market: Optional[str] = None
    asset_class: Optional[str] = None
    symbol: str = Field(..., min_length=1)
    name: Optional[str] = None
    currency: Optional[str] = None
    status: Optional[str] = None
    latest_trade_date: Optional[date] = None
    latest_price: Optional[Decimal] = None


class InstrumentCacheDocument(BaseModel):
    """Full generated instruments.json document."""

    generated_at: str
    total: int = Field(..., ge=0)
    markets: list[str]
    asset_classes: list[str]
    data: list[InstrumentCacheItem]


class InstrumentCacheReplaceRequest(InstrumentCacheDocument):
    """PUT body for replacing the generated instruments.json document."""

    pass


class InstrumentCacheItemPatchRequest(BaseModel):
    """PATCH body for editing a single cached instrument."""

    market: Optional[str] = None
    asset_class: Optional[str] = None
    symbol: Optional[str] = Field(default=None, min_length=1)
    name: Optional[str] = None
    currency: Optional[str] = None
    status: Optional[str] = None
    latest_trade_date: Optional[date] = None
    latest_price: Optional[Decimal] = None

    model_config = ConfigDict(extra="forbid")


class InstrumentCacheWriteResponse(BaseModel):
    """Response after writing instruments.json."""

    success: bool = True
    message: str
    data: InstrumentCacheDocument


# ── refresh_instrument_cache ──────────────────────────────────────────────────────────
class InstrumentCacheItemUpdateResponse(BaseModel):
    """Response after updating a single cached instrument."""

    success: bool = True
    message: str
    data: InstrumentCacheItem

class CacheTriggerResponse(BaseModel):
    message: str
    status: str