"""Versioned daily configuration publication; never an acquisition grant."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.execution_profiles import ExecutionProfile
from app.provider_catalog import BoundedMonitor, CompletionDeadline, ContractDefinition


class ExecutionConfigQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_profile: ExecutionProfile
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def query_version(cls, value: object) -> object:
        return 1 if value == "1" else value


class FeedSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    dataset_key: str
    market: str
    calendar_market: str
    asset_class: str
    frequency: Literal["daily"]
    contract: ContractDefinition
    scheduled_time: str
    source_timezone: str
    target_date_lag_days: int
    publication_retry_window_seconds: int
    completion_deadline: CompletionDeadline
    bounded_monitor: BoundedMonitor


class FeedScheduleView(FeedSchedule):
    next_scheduled_at: datetime


class ExecutionConfigResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    environment: Literal["local", "staging", "production"]
    execution_profile: ExecutionProfile
    provider: str
    schedule_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    feeds: list[FeedSchedule] = Field(min_length=1)
