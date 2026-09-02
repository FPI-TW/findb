"""Pydantic schemas for the versioned Source API surface."""

from datetime import date, datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.slot_identity import CanonicalSlotId
from app.utils import ensure_utc


class IngestResponse(BaseModel):
    """Stable response used by contract reruns."""

    success: bool
    run_id: UUID
    status: str
    message: str


class SchedulerControlPollRequest(BaseModel):
    """Fetcher heartbeat and observed-state report."""

    observed_state: Literal["running", "stopped"]
    cycle_started_at: datetime | None = None
    cycle_completed_at: datetime | None = None
    last_error: str | None = Field(default=None, max_length=4000)

    @field_validator("cycle_started_at", "cycle_completed_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None


class SchedulerControlPollResponse(BaseModel):
    success: Literal[True] = True
    scheduler_key: str
    provider: str
    dataset_keys: list[str]
    slot_id: CanonicalSlotId
    scheduled_local_time: time
    timezone: str
    desired_state: Literal["running", "stopped"]
    revision: int
    server_time: datetime


# Alias retained for callers that refer to this operation as a heartbeat.
SchedulerHeartbeatRequest = SchedulerControlPollRequest


class CanonicalIngestResponse(BaseModel):
    """Accepted response for the versioned canonical ingest endpoint."""

    success: Literal[True] = True
    attempt_id: UUID
    run_id: UUID
    status: str
    schema_id: str
    schema_version: int
    message: str


class IngressErrorDetail(BaseModel):
    """Stable machine-readable canonical ingest error."""

    code: str
    message: str


class IngressErrorResponse(BaseModel):
    """Canonical ingest error; lineage is absent if attempt persistence failed."""

    success: Literal[False] = False
    attempt_id: UUID | None = None
    error: IngressErrorDetail


class IngestionAttemptResponse(BaseModel):
    """Source-client-scoped canonical ingestion attempt status."""

    model_config = ConfigDict(from_attributes=True)

    attempt_id: UUID
    run_id: UUID | None = None
    dataset_key: str | None = None
    source: str | None = None
    schema_id: str | None = None
    schema_version: int | None = None
    request_key: str | None = None
    idempotency_key: str | None = None
    status: str
    http_status: int | None = None
    failure_code: str | None = None
    error_message: str | None = None
    failure_details: dict[str, Any] | None = None
    created_at: datetime
    completed_at: datetime | None = None


class RunStatusResponse(BaseModel):
    """匯入執行狀態查詢回應。"""

    run_id: UUID
    dataset_key: str
    schema_id: str | None = None
    schema_version: int | None = None
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    total_records: int = 0
    success_records: int = 0
    failed_records: int = 0
    error_message: str | None = None
    failure_code: str | None = None
    attempt_count: int = 0
    max_attempts: int = 5
    next_retry_at: datetime | None = None


class DatasetInfo(BaseModel):
    """資料集資訊。"""

    dataset_key: str
    name: str
    description: str | None = None
    asset_class: str
    market: str
    frequency: str
    is_active: bool
    schema_id: str | None = None
    accepted_schema_versions: list[int] = Field(default_factory=list)
    current_schema_version: int | None = None
    schema_enforcement: str | None = None


class DatasetListResponse(BaseModel):
    """資料集列表回應。"""

    success: bool = True
    data: list[DatasetInfo]


class HistoricalBackfillClaimResponse(BaseModel):
    """Provider-neutral work item. It deliberately contains no credentials."""

    success: Literal[True] = True
    item_id: UUID | None = None
    request_id: UUID | None = None
    request_key: str | None = None
    provider: str | None = None
    dataset_key: str | None = None
    market: str | None = None
    trade_date: date | None = None
    lease_token: UUID | None = None


class HistoricalBackfillItemUpdateRequest(BaseModel):
    status: Literal["completed", "failed"]
    lease_token: UUID
    run_id: UUID | None = None
    failure_code: str | None = Field(default=None, max_length=50)
    failure_message: str | None = Field(default=None, max_length=1000)
