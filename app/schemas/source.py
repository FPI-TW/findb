"""
Source API Pydantic schemas.
"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


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
