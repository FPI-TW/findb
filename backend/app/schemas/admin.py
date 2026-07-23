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
    schema_id: Optional[str] = None
    schema_version: Optional[int] = None
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


class SourceClientCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    source_name: str = Field(..., min_length=1, max_length=50)
    allowed_datasets: Optional[list[str]] = None
    rate_limit_requests: int = Field(default=100, ge=1)
    rate_limit_window: int = Field(default=60, ge=1)


class SourceClientResponse(BaseModel):
    client_id: UUID
    name: str
    source_name: str
    allowed_datasets: Optional[list[str]] = None
    rate_limit_requests: int
    rate_limit_window: int
    created_at: datetime
    updated_at: datetime
    revoked_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class SourceClientCreateResponse(BaseModel):
    success: bool = True
    api_key: str
    data: SourceClientResponse


class SourceClientListResponse(BaseModel):
    success: bool = True
    data: list[SourceClientResponse]


class QueueHealthResponse(BaseModel):
    counts: dict[str, int]
    oldest_queued_at: Optional[datetime] = None
    oldest_queued_age_seconds: Optional[float] = None
    unpublished_outbox: int
    oldest_unpublished_outbox_at: Optional[datetime] = None
    oldest_unpublished_outbox_age_seconds: Optional[float] = None
    expired_leases: int
    retry_exhausted: int
    last_worker_heartbeat_at: Optional[datetime] = None
    worker_heartbeat_age_seconds: Optional[float] = None
    missing_deliveries: int
    oldest_missing_delivery_at: Optional[datetime] = None
    oldest_missing_delivery_age_seconds: Optional[float] = None


class MissingDeliveryAlertResponse(BaseModel):
    alert_id: UUID
    dataset_key: str
    source: str
    schema_id: str
    schema_version: int
    expected_data_date: date
    status: str
    first_detected_at: datetime
    last_detected_at: datetime
    resolved_at: Optional[datetime] = None
    details: Optional[dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


class MissingDeliveryAlertListResponse(PaginatedResponse[MissingDeliveryAlertResponse]):
    pass


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


# ── API Keys ──────────────────────────────────────────────────────────────────


class APIKeyCreateRequest(BaseModel):
    owner: str = Field(..., min_length=1, max_length=100)
    tier: str = Field(default="standard", min_length=1, max_length=30)
    scopes: list[str] = Field(default_factory=lambda: ["serve"])
    rate_limit_requests: int = Field(default=100, ge=1)
    rate_limit_window: int = Field(default=60, ge=1)
    page_size_limit: int = Field(default=1000, ge=1, le=1000)


class APIKeyResponse(BaseModel):
    key_id: UUID
    owner: str
    tier: str
    scopes: list[str]
    rate_limit_requests: int
    rate_limit_window: int
    page_size_limit: int
    usage_count: int
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class APIKeyCreateResponse(BaseModel):
    success: bool = True
    api_key: str
    data: APIKeyResponse


class APIKeyListResponse(BaseModel):
    success: bool = True
    data: list[APIKeyResponse]
