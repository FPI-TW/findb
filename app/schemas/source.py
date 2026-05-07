"""
Source API Pydantic schemas.
"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class IngestRequest(BaseModel):
    """Request body for data ingestion."""

    dataset_key: str = Field(
        ...,
        min_length=1,
        description="Dataset identifier (e.g., crypto_eod)",
    )
    source: str = Field(
        ...,
        min_length=1,
        description="Data source (e.g., bloomberg)",
    )
    request_key: str = Field(
        ...,
        min_length=1,
        description="Upstream request identifier for tracing",
    )
    idempotency_key: str = Field(
        ...,
        min_length=1,
        description="Idempotency key for request deduplication",
    )
    payload: dict[str, Any] = Field(..., description="Raw data payload")
    fetched_at: datetime = Field(..., description="Timestamp when data was fetched")


class DirectIngestPayload(BaseModel):
    """Direct market payload from fetch layer (metadata + data)."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    data: list[dict[str, Any]] = Field(default_factory=list)


def _normalize_optional_symbol(value: Optional[str]) -> Optional[str]:
    """Normalize symbol-like inputs so blank strings are treated as missing."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class TWStockDirectMetadata(BaseModel):
    """Metadata for TW MultiCharts direct ingest payloads."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    symbol: Optional[str] = Field(default=None, description="台股代號，例如 6160")
    name: Optional[str] = Field(default=None, description="股票名稱")
    source: str = Field(default="multicharts", description="資料來源，預設 multicharts")
    file_name: Optional[str] = Field(default=None, description="來源檔名，用於追蹤")
    query_time: Optional[datetime] = Field(default=None, description="匯入查詢時間（UTC）")

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_optional_symbol(value)


class TWStockDirectRow(BaseModel):
    """Single TW MultiCharts direct ingest row."""

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
    up_volume: Optional[int] = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("up_volume", "UpVolume", "<UpVolume>"),
    )
    down_volume: Optional[int] = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("down_volume", "DownVolume", "<DownVolume>"),
    )
    total_volume: Optional[int] = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("total_volume", "TotalVolume", "<TotalVolume>", "volume"),
    )
    up_ticks: Optional[int] = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("up_ticks", "UpTicks", "<UpTicks>"),
    )
    down_ticks: Optional[int] = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("down_ticks", "DownTicks", "<DownTicks>"),
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
    """TW MultiCharts direct ingest payload."""

    model_config = ConfigDict(populate_by_name=True)

    metadata: TWStockDirectMetadata = Field(default_factory=TWStockDirectMetadata)
    data: list[TWStockDirectRow] = Field(default_factory=list)

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
            "up_volume",
            "down_volume",
            "total_volume",
            "up_ticks",
            "down_ticks",
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
    """Response for data ingestion."""

    success: bool
    run_id: UUID
    status: str
    message: str


class RunStatusResponse(BaseModel):
    """Response for run status query."""

    run_id: UUID
    dataset_key: str
    status: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_records: int = 0
    success_records: int = 0
    failed_records: int = 0
    error_message: Optional[str] = None


class DatasetInfo(BaseModel):
    """Dataset information."""

    dataset_key: str
    name: str
    description: Optional[str] = None
    asset_class: str
    market: str
    frequency: str
    is_active: bool


class DatasetListResponse(BaseModel):
    """Response for dataset listing."""

    success: bool = True
    data: list[DatasetInfo]
