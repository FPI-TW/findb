"""Admin API 使用的 Pydantic schema。"""

from base64 import b64decode
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal, Optional
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import PaginatedResponse
from app.utils import ensure_utc, utc_now

# ── Managed trading calendars ───────────────────────────────────────────────


CalendarDayStatus = Literal["open", "closed", "settlement_only"]


class CalendarMarketRequest(BaseModel):
    market: str = Field(min_length=1, max_length=10)
    display_name: str = Field(min_length=1, max_length=100)
    timezone: str = Field(min_length=1, max_length=64)
    weekend_days: list[int] = Field(default_factory=lambda: [5, 6], max_length=7)
    default_session_open: Optional[time] = None
    default_session_close: Optional[time] = None
    active: bool = True

    @field_validator("weekend_days")
    @classmethod
    def validate_weekend_days(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("weekend_days must contain ISO weekday indexes 0 through 6")
        return sorted(set(value))

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class CalendarMarketResponse(CalendarMarketRequest):
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class CalendarDayInput(BaseModel):
    trade_date: date
    status: CalendarDayStatus
    holiday_name: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = Field(default=None, max_length=2000)
    session_open: Optional[time] = None
    session_close: Optional[time] = None


class CalendarJsonPreviewRequest(BaseModel):
    market: str = Field(min_length=1, max_length=10)
    year: int = Field(ge=1900, le=2200)
    coverage_mode: Literal["exceptions", "full_year"] = "exceptions"
    days: list[CalendarDayInput] = Field(max_length=366)
    source_filename: Optional[str] = Field(default=None, max_length=255)


class CalendarCsvPreviewRequest(BaseModel):
    """Base64 transport avoids retaining multipart uploads and caps request size."""

    market: str = Field(min_length=1, max_length=10)
    year: int = Field(ge=1900, le=2200)
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=1_400_000)

    @field_validator("content_base64")
    @classmethod
    def validate_base64(cls, value: str) -> str:
        try:
            if len(b64decode(value, validate=True)) > 1_000_000:
                raise ValueError("CSV exceeds 1 MiB")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("content_base64 must be valid base64") from exc
        return value


class CalendarPreviewResponse(BaseModel):
    batch_id: UUID
    market: str
    year: int
    input_format: str
    detected_encoding: Optional[str] = None
    base_revision: int
    summary: dict[str, int]
    warnings: list[str]
    errors: list[str]
    days: list[CalendarDayInput]
    expires_at: datetime


class CalendarApplyRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class CalendarMutationRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    status: CalendarDayStatus
    holiday_name: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = Field(default=None, max_length=2000)
    session_open: Optional[time] = None
    session_close: Optional[time] = None
    reason: str = Field(min_length=1, max_length=500)


class CalendarPublishRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class CalendarRollbackRequest(BaseModel):
    target_revision: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class CalendarRevisionResponse(BaseModel):
    market: str
    year: int
    revision: int
    status: Literal["draft", "published", "superseded"]
    expected_days: int
    actual_days: int
    timezone: str
    source_kind: str
    source_filename: Optional[str] = None
    published_at: Optional[datetime] = None
    updated_at: datetime
    coverage_complete: bool


class CalendarManagedDayResponse(CalendarDayInput):
    market: str
    is_open: bool
    revision: int
    source_kind: str


class CalendarYearResponse(BaseModel):
    revision: CalendarRevisionResponse
    days: list[CalendarManagedDayResponse]
    published_revision: CalendarRevisionResponse | None = None


class CalendarImportResponse(BaseModel):
    id: UUID
    market: str
    year: int
    input_format: str
    source_filename: Optional[str] = None
    source_sha256: Optional[str] = None
    base_revision: int
    status: str
    revision: Optional[int] = None
    created_by: Optional[str] = None
    created_at: datetime
    expires_at: datetime

    model_config = ConfigDict(from_attributes=True)


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
    source: Optional[str] = None
    provider: Optional[str] = None
    dataset_key: Optional[str] = None
    schema_id: Optional[str] = None
    schema_version: Optional[int] = None
    raw_payload_id: Optional[UUID] = None
    raw_available: bool = False
    fetched_at: Optional[datetime] = None
    request_key: Optional[str] = None
    batch_data_date: Optional[date] = None
    policy_detail: Optional[dict[str, Any]] = None
    resolved: bool
    resolved_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, extra="ignore")


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
    owner: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = None
    expires_at: Optional[datetime] = None

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        value = ensure_utc(value)
        if value <= utc_now():
            raise ValueError("expires_at must be in the future")
        return value


class SourceClientResponse(BaseModel):
    client_id: UUID
    name: str
    source_name: str
    owner: Optional[str] = None
    description: Optional[str] = None
    fingerprint: Optional[str] = None
    allowed_datasets: Optional[list[str]] = None
    rate_limit_requests: int
    rate_limit_window: int
    created_at: datetime
    updated_at: datetime
    revoked_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    rotated_from_id: Optional[UUID] = None
    usage_count: int = 0
    last_used_at: Optional[datetime] = None

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


class MarketFreshnessFeedResponse(BaseModel):
    dataset_key: str
    source: str
    schema_id: Optional[str] = None
    schema_version: Optional[int] = None
    expected_data_date: Optional[date] = None
    latest_successful_data_date: Optional[date] = None
    last_fetched_at: Optional[datetime] = None
    last_completed_at: Optional[datetime] = None
    last_run_id: Optional[UUID] = None
    total_records: Optional[int] = None
    success_records: Optional[int] = None
    failed_records: Optional[int] = None
    policy_outcome: Optional[str] = None
    open_missing_delivery_alert: bool
    last_failure_code: Optional[str] = None
    configuration_error: Optional[str] = None
    status: Literal["not_due", "fresh", "partial", "late", "failed", "never_received"]


class MarketFreshnessResponse(BaseModel):
    market: str
    scheduler_key: str
    provider: str
    dataset_keys: list[str] = Field(default_factory=list)
    slot_id: str
    scheduled_local_time: time
    timezone: str
    desired_state: Literal["running", "stopped"]
    observed_state: Literal["running", "stopped"]
    revision: int
    last_heartbeat_at: Optional[datetime] = None
    last_cycle_started_at: Optional[datetime] = None
    last_cycle_completed_at: Optional[datetime] = None
    last_error: Optional[str] = None
    heartbeat_age_seconds: Optional[float] = None
    configuration_status: Literal["ready", "error"] = "ready"
    configuration_errors: list[str] = Field(default_factory=list)
    status: Literal["not_due", "fresh", "partial", "late", "failed", "never_received"]
    expected_data_date: Optional[date] = None
    coverage_data_date: Optional[date] = None
    last_fetched_at: Optional[datetime] = None
    last_successful_update_at: Optional[datetime] = None
    last_complete_at: Optional[datetime] = None
    next_scheduled_at: datetime
    feed_count: int
    fresh_feed_count: int
    late_feed_count: int
    feeds: list[MarketFreshnessFeedResponse] = Field(default_factory=list)


class MarketFreshnessListResponse(BaseModel):
    success: bool = True
    data: list[MarketFreshnessResponse]


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
    name: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = None
    expires_at: Optional[datetime] = None

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        value = ensure_utc(value)
        if value <= utc_now():
            raise ValueError("expires_at must be in the future")
        return value


class APIKeyResponse(BaseModel):
    key_id: UUID
    kind: str = "serve"
    name: Optional[str] = None
    owner: str
    description: Optional[str] = None
    fingerprint: Optional[str] = None
    role: Optional[str] = None
    tier: str
    scopes: list[str]
    rate_limit_requests: int
    rate_limit_window: int
    page_size_limit: int
    usage_count: int
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    rotated_from_id: Optional[UUID] = None

    model_config = ConfigDict(from_attributes=True)


class APIKeyCreateResponse(BaseModel):
    success: bool = True
    api_key: str
    data: APIKeyResponse


class APIKeyListResponse(BaseModel):
    success: bool = True
    data: list[APIKeyResponse]


# ── Admin identity and unified credentials ───────────────────────────────────


class AdminUserResponse(BaseModel):
    user_id: UUID
    username: str
    display_name: str
    role: Literal["owner", "operator", "viewer"]
    is_active: bool
    must_change_password: bool
    created_at: datetime
    updated_at: datetime
    disabled_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    password_changed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class BootstrapRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=12, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=256)


class SessionResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: AdminUserResponse


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=12, max_length=256)


class SuccessResponse(BaseModel):
    success: bool = True


class SchedulerControlResponse(BaseModel):
    """A persisted scheduler control row plus its current heartbeat age."""

    scheduler_key: str
    provider: str
    dataset_keys: list[str]
    slot_id: str
    scheduled_local_time: time
    timezone: str
    desired_state: Literal["running", "stopped"]
    observed_state: Literal["running", "stopped"]
    revision: int
    last_heartbeat_at: Optional[datetime] = None
    last_cycle_started_at: Optional[datetime] = None
    last_cycle_completed_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    heartbeat_age_seconds: Optional[float] = None

    model_config = ConfigDict(from_attributes=True)


class SchedulerControlListResponse(BaseModel):
    success: bool = True
    data: list[SchedulerControlResponse]


class SchedulerControlMutationResponse(BaseModel):
    success: bool = True
    data: SchedulerControlResponse


class SchedulerControlUpdateRequest(BaseModel):
    desired_state: Literal["running", "stopped"]
    expected_revision: int = Field(ge=0)


# Short aliases make the control-plane schemas convenient for callers while
# preserving the explicit names used by the API handlers.
SchedulerUpdateRequest = SchedulerControlUpdateRequest


class AdminUserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=100)
    role: Literal["owner", "operator", "viewer"]
    password: Optional[str] = Field(default=None, min_length=12, max_length=256)
    must_change_password: bool = True


class AdminUserUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    role: Optional[Literal["owner", "operator", "viewer"]] = None
    is_active: Optional[bool] = None


class PasswordResetRequest(BaseModel):
    password: Optional[str] = Field(default=None, min_length=12, max_length=256)


class AdminUserEnvelope(BaseModel):
    data: AdminUserResponse
    temporary_password: Optional[str] = None


class AdminUserListResponse(BaseModel):
    data: list[AdminUserResponse]


class CredentialCreateRequest(BaseModel):
    kind: Literal["source", "serve", "admin"]
    name: Optional[str] = Field(default=None, max_length=100)
    owner: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = None
    expires_at: Optional[datetime] = None
    source_name: Optional[str] = Field(default=None, max_length=50)
    allowed_datasets: Optional[list[str]] = None
    role: Optional[Literal["owner", "operator", "viewer"]] = None
    tier: str = "standard"
    scopes: Optional[list[str]] = None
    rate_limit_requests: int = Field(default=100, ge=1)
    rate_limit_window: int = Field(default=60, ge=1)
    page_size_limit: int = Field(default=1000, ge=1, le=1000)

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        value = ensure_utc(value)
        if value <= utc_now():
            raise ValueError("expires_at must be in the future")
        return value


class CredentialResponse(BaseModel):
    credential_ref: str
    id: Optional[UUID] = None
    kind: str
    name: str
    owner: Optional[str] = None
    description: Optional[str] = None
    status: Literal["active", "expiring", "expired", "revoked"]
    fingerprint: Optional[str] = None
    role: Optional[str] = None
    scopes: Optional[list[str]] = None
    policies: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    usage_count: int = 0
    rotated_from_id: Optional[UUID] = None


class CredentialCreateResponse(BaseModel):
    api_key: str
    data: CredentialResponse


class CredentialListResponse(BaseModel):
    data: list[CredentialResponse]


class CredentialOverviewResponse(BaseModel):
    auth_mode: Literal["db_only"]
    serve_require_auth: bool
    counts: dict[str, int]
    auth_failure_counts: dict[str, int] = Field(default_factory=dict)
    usage_updated_at: Optional[datetime] = None
