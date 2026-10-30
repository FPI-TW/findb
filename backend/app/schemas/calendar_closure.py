"""Owner-reviewed retirement of empty, calendar-skipped daily work."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ClosureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalendarClosureCheckpoint(ClosureModel):
    observed_at: AwareDatetime
    host_lock_identity: str = Field(min_length=3, max_length=200)
    inventory_sha256: Digest
    facts: dict[str, Any]
    sha256: Digest


class CalendarClosureRequest(ClosureModel):
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    environment: Literal["local", "staging", "production"]
    execution_profile: Literal["pilot", "full_market"]
    plan_id: UUID
    reason: str = Field(min_length=10, max_length=2000)
    checkpoint: CalendarClosureCheckpoint


class CalendarClosureApplyRequest(CalendarClosureRequest):
    expected_sha256: Digest


class CalendarClosureInventory(ClosureModel):
    work_id: UUID
    environment: str
    execution_profile: str
    plan_id: UUID
    schema_ready: bool
    snapshot: dict[str, Any]
    sha256: Digest


class CalendarClosurePreview(ClosureModel):
    inventory: CalendarClosureInventory
    request: CalendarClosureRequest
    apply_ready: bool
    blockers: list[str]
    proposed_state: Literal["calendar_closed"] = "calendar_closed"
    sha256: Digest


class CalendarClosureResult(ClosureModel):
    work_id: UUID
    closure_id: UUID
    state: Literal["calendar_closed"] = "calendar_closed"
    closed_at: datetime
    replayed: bool
