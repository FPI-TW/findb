"""Source API 使用的 Pydantic schema。"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


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


class IngestRequestV2(BaseModel):
    """資料匯入請求內容，api v2系列用。"""

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
    message_id: str = Field(
        ..., min_length=1, description="訊息佇列 (Queue) 中的唯一識別碼，避免重複處理相同請求"
    )
    payload: dict[str, Any] = Field(..., description="原始資料內容")
    fetched_at: datetime = Field(..., description="資料抓取時間")


class OhlcvDataItem(BaseModel):
    """
    股票市場數據項（OHLCV 格式）
    """

    ticker: str = Field(..., description="標的代碼，例如 BTC")
    date: str = Field(..., description="交易日期 (YYYY-MM-DD)")
    open: float = Field(..., description="開盤價")
    high: float = Field(..., description="最高價")
    low: float = Field(..., description="最低價")
    close: float = Field(..., description="收盤價")
    volume: float = Field(default=0, description="成交量")


class OhlcvIngestPayload(BaseModel):
    """資料載體，包含數據列表"""

    data: list[OhlcvDataItem]


class IngestEquitieRequest(BaseModel):
    """最外層的完整請求結構"""

    dataset_key: str = Field(..., min_length=1, description="資料集金鑰，如 crypto_eod")
    source: str = Field(..., min_length=1, description="來源名稱")
    request_key: str = Field(..., min_length=1, description="請求唯一識別碼")
    idempotency_key: str = Field(..., min_length=1, description="冪等鍵，用於避免重複處理相同請求")
    payload: OhlcvIngestPayload = Field(..., description="實際數據內容")
    fetched_at: datetime = Field(..., description="抓取時間")


class MarketDataItem(BaseModel):
    """
    通用型市場數據項。
    適用於：美股、加密貨幣、台股等具備 OHLCV 結構的資料。
    """

    ticker: str = Field(..., description="Symbol")
    date: str = Field(..., description="YYYY-MM-DD")
    open: float
    high: float
    low: float
    close: float
    volume: int = Field(default=0)


class DirectIngestPayload(BaseModel):
    """fetch 層直接送入的市場資料內容，包含 metadata 與 data。"""

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
    """資料匯入回應。"""

    success: bool
    run_id: UUID
    status: str
    message: str


class IngestQueueResponse(BaseModel):
    """資料進入Queue之回應"""

    message_id: UUID
    request_key: str
    status: str = "queued"


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
