"""Source API 使用的 Pydantic schema。"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


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


class DirectIngestPayload(BaseModel):
    """fetch 層直接送入的市場資料內容，包含 metadata 與 data。"""

    metadata: dict[str, Any] = Field(default_factory=dict)
    data: list[dict[str, Any]] = Field(default_factory=list)


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
