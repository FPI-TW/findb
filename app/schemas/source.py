"""Source API 使用的 Pydantic schema。"""

import json
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import get_settings


def _current_source_max_payload_bytes() -> int:
    settings = get_settings()
    return max(1, int(settings.SOURCE_MAX_PAYLOAD_BYTES))


def _current_source_max_data_items() -> int:
    settings = get_settings()
    return max(1, int(settings.SOURCE_MAX_DATA_ITEMS))


def _payload_size_bytes(payload: Any) -> int:
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return len(serialized.encode("utf-8"))


def ensure_payload_size_within_limit(payload: Any) -> None:
    max_bytes = _current_source_max_payload_bytes()
    size_bytes = _payload_size_bytes(payload)
    if size_bytes > max_bytes:
        raise ValueError(f"Payload exceeds maximum size of {max_bytes} bytes")


def ensure_data_items_count_within_limit(item_count: int) -> None:
    max_items = _current_source_max_data_items()
    if item_count > max_items:
        raise ValueError(f"Payload data list exceeds maximum size of {max_items} items")


class IngestRequest(BaseModel):
    """資料匯入請求內容。"""

    dataset_key: str = Field(
        ...,
        min_length=1,
        description="資料集識別碼，例如 crypto_eod",
    )
    source: str = Field(
        ...,
        min_length=1,
        description="資料來源，例如 bloomberg",
    )
    request_key: str = Field(
        ...,
        min_length=1,
        description="上游請求識別碼，用於追蹤資料來源",
    )
    idempotency_key: str = Field(
        ...,
        min_length=1,
        description="冪等鍵，用於避免重複處理相同請求",
    )
    payload: dict[str, Any] = Field(..., description="原始資料內容")
    fetched_at: datetime = Field(..., description="資料抓取時間")

    @model_validator(mode="after")
    def validate_payload_limits(self) -> "IngestRequest":
        ensure_payload_size_within_limit(self.payload)
        data_items = self.payload.get("data")
        if isinstance(data_items, list):
            ensure_data_items_count_within_limit(len(data_items))
        return self


class DirectIngestPayload(BaseModel):
    """fetch 層直接送入的市場資料內容，包含 metadata 與 data。"""

    metadata: dict[str, Any] = Field(default_factory=dict)
    data: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_payload_limits(self) -> "DirectIngestPayload":
        ensure_data_items_count_within_limit(len(self.data))
        ensure_payload_size_within_limit(self.model_dump(mode="json"))
        return self


def _normalize_optional_symbol(value: Optional[str]) -> Optional[str]:
    """Normalize symbol-like inputs so blank strings are treated as missing."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


_TWSTOCK_ASSET_CLASS_ALIASES = {
    "stock": "equity",
    "equity": "equity",
    "etf": "etf",
}


class TWStockDirectMetadata(BaseModel):
    """Metadata for TW stock direct ingest payloads (FinLab format)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    symbol: Optional[str] = Field(default=None, description="台股代號，例如 6160")
    name: Optional[str] = Field(default=None, description="股票名稱")
    source: str = Field(default="finlab", description="資料來源，預設 finlab")
    asset_class: Optional[str] = Field(
        default=None,
        description="資產類別，可選 stock/equity/etf；空值預設視為 equity（大小寫不敏感）",
    )
    file_name: Optional[str] = Field(default=None, description="來源檔名，用於追蹤")
    query_time: Optional[datetime] = Field(default=None, description="匯入查詢時間（UTC）")

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_optional_symbol(value)

    @field_validator("asset_class", mode="before")
    @classmethod
    def normalize_asset_class(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        asset_class = str(value).strip().lower()
        if not asset_class:
            return None
        if asset_class not in _TWSTOCK_ASSET_CLASS_ALIASES:
            allowed = ", ".join(sorted(_TWSTOCK_ASSET_CLASS_ALIASES))
            raise ValueError(f"asset_class must be one of: {allowed}")
        return _TWSTOCK_ASSET_CLASS_ALIASES[asset_class]


class TWStockDirectRow(BaseModel):
    """Single TW stock direct ingest row (FinLab format)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    date: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("date", "Date", "<Date>"),
        description="交易日期，YYYY-MM-DD",
    )
    symbol: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("symbol", "Symbol", "<Symbol>"),
        description="台股代號，例如 6160",
    )
    time: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("time", "Time", "<Time>"),
        description="交易時間，例如 13:30:00",
    )
    open: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("open", "Open", "<Open>"),
    )
    high: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("high", "High", "<High>"),
    )
    low: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("low", "Low", "<Low>"),
    )
    close: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("close", "Close", "<Close>"),
    )
    total_volume: Optional[int] = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("total_volume", "TotalVolume", "<TotalVolume>", "volume"),
    )
    total_ticks: Optional[int] = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("total_ticks", "TotalTicks", "<TotalTicks>"),
    )
    open_interest: Optional[int] = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices(
            "open_interest",
            "OpenInterest",
            "<OpenInterest>",
        ),
        description="接受後忽略，不會寫入 canonical",
    )

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_optional_symbol(value)


class TWStockDirectIngestPayload(BaseModel):
    """TW stock direct ingest payload (FinLab format)."""

    model_config = ConfigDict(populate_by_name=True)

    metadata: TWStockDirectMetadata = Field(default_factory=TWStockDirectMetadata)
    data: list[TWStockDirectRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_payload_limits(self) -> "TWStockDirectIngestPayload":
        ensure_data_items_count_within_limit(len(self.data))
        ensure_payload_size_within_limit(self.model_dump(mode="json"))
        return self

    def validate_required_fields(self) -> None:
        """Apply route-level business validation with 400-friendly errors."""
        metadata_symbol = self.metadata.symbol
        row_symbols = [row.symbol for row in self.data]
        if not metadata_symbol and not any(row_symbols):
            raise ValueError("metadata.symbol or data[].symbol is required")

        required_fields = (
            "date",
            "open",
            "high",
            "low",
            "close",
            "total_volume",
            "total_ticks",
        )

        for index, row in enumerate(self.data):
            missing_fields = [
                field_name for field_name in required_fields if getattr(row, field_name) is None
            ]
            if not metadata_symbol and not row_symbols[index]:
                missing_fields.append("symbol")
            if missing_fields:
                joined = ", ".join(missing_fields)
                raise ValueError(f"data[{index}] missing required fields: {joined}")


class IngestResponse(BaseModel):
    """資料匯入回應。"""

    success: bool
    run_id: UUID
    status: str
    message: str


class RunStatusResponse(BaseModel):
    """匯入執行狀態查詢回應。"""

    run_id: UUID
    dataset_key: str
    status: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_records: int = 0
    success_records: int = 0
    failed_records: int = 0
    error_message: Optional[str] = None
    failure_code: Optional[str] = None
    attempt_count: int = 0
    max_attempts: int = 5
    next_retry_at: Optional[datetime] = None


class DatasetInfo(BaseModel):
    """資料集資訊。"""

    dataset_key: str
    name: str
    description: Optional[str] = None
    asset_class: str
    market: str
    frequency: str
    is_active: bool


class DatasetListResponse(BaseModel):
    """資料集列表回應。"""

    success: bool = True
    data: list[DatasetInfo]
