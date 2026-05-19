"""Admin API 使用的 Pydantic schema。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PaginatedResponse

# ── EOD Correction ────────────────────────────────────────────────────────────


class PatchEODRequest(BaseModel):
    """
    MarketDataEOD 修正請求內容。

    只會更新明確提供的 OHLCV 欄位，且必須提供 correction_reason。
    """

    correction_reason: str = Field(..., min_length=1, max_length=2000)
    open: Optional[Decimal] = Field(default=None, ge=0)
    high: Optional[Decimal] = Field(default=None, ge=0)
    low: Optional[Decimal] = Field(default=None, ge=0)
    close: Optional[Decimal] = Field(default=None, ge=0)
    volume: Optional[int] = Field(default=None, ge=0)
    total_ticks: Optional[int] = Field(default=None, ge=0)
    turnover: Optional[Decimal] = Field(default=None, ge=0)


class PatchEODResponse(BaseModel):
    """日線資料修正後的回應。"""

    success: bool = True
    correction_id: UUID
    record_id: UUID
    instrument_id: UUID
    trade_date: date
    message: str

    model_config = ConfigDict(from_attributes=True)


# ── DQ Issue Resolution ────────────────────────────────────────────────────────


class DQIssueResponse(BaseModel):
    """單一資料品質問題紀錄。"""

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
    """分頁資料品質問題列表。"""

    pass


class ResolveDQIssueRequest(BaseModel):
    """資料品質問題標記為已解決的請求內容。"""

    correction_reason: str = Field(..., min_length=1, max_length=2000)


class ResolveDQIssueResponse(BaseModel):
    """資料品質問題解決後的回應。"""

    success: bool = True
    correction_id: UUID
    issue_id: UUID
    resolved_at: datetime
    message: str

    model_config = ConfigDict(from_attributes=True)


# ── Correction List ────────────────────────────────────────────────────────────


class CorrectionResponse(BaseModel):
    """單一修正稽核紀錄。"""

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
    """分頁修正稽核列表。"""

    pass


# ── Raw Payload ────────────────────────────────────────────────────────────────


class RawPayloadResponse(BaseModel):
    """單一原始市場資料紀錄。"""

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
    """分頁原始資料列表。"""

    pass


class BulkRerunResponse(BaseModel):
    """批次重新執行回應。"""

    queued: int
    skipped: int
    errors: int
    new_run_ids: list[str]
    error_details: list[str]


# ── Instrument Cache ──────────────────────────────────────────────────────────


class InstrumentCacheItem(BaseModel):
    """產生後的靜態商品快取單一項目。"""

    instrument_id: str = Field(..., min_length=1)
    market: Optional[str] = None
    asset_class: Optional[str] = None
    symbol: str = Field(..., min_length=1)
    name: Optional[str] = None
    short_name: Optional[str] = None
    currency: Optional[str] = None
    status: Optional[str] = None
    first_trade_date: Optional[date] = None
    latest_trade_date: Optional[date] = None
    latest_price: Optional[Decimal] = None


class InstrumentCacheDocument(BaseModel):
    """完整產生後的 instruments.json 文件。"""

    generated_at: str
    total: int = Field(..., ge=0)
    markets: list[str]
    asset_classes: list[str]
    data: list[InstrumentCacheItem]


class InstrumentCacheReplaceRequest(InstrumentCacheDocument):
    """替換 instruments.json 文件的請求內容。"""

    pass


class InstrumentCacheItemPatchRequest(BaseModel):
    """編輯單一快取商品的請求內容。"""

    market: Optional[str] = None
    asset_class: Optional[str] = None
    symbol: Optional[str] = Field(default=None, min_length=1)
    name: Optional[str] = None
    short_name: Optional[str] = None
    currency: Optional[str] = None
    status: Optional[str] = None
    first_trade_date: Optional[date] = None
    latest_trade_date: Optional[date] = None
    latest_price: Optional[Decimal] = None

    model_config = ConfigDict(extra="forbid")


class InstrumentCacheWriteResponse(BaseModel):
    """寫入 instruments.json 後的回應。"""

    success: bool = True
    message: str
    data: InstrumentCacheDocument


class InstrumentCacheItemUpdateResponse(BaseModel):
    """更新單一快取商品後的回應。"""

    success: bool = True
    message: str
    data: InstrumentCacheItem


# ── refresh_instrument_cache ──────────────────────────────────────────────────────────


class CacheTriggerResponse(BaseModel):
    message: str
    status: str
