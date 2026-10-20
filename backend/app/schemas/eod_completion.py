"""Explicit whole-scope outcomes for EOD v2; gaps never become no-data proof."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EODRawEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1, max_length=2048, pattern=r"^r2://[0-9a-f]{32}/")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def content_addressed(self) -> "EODRawEvidence":
        if any(value in self.ref for value in ("?", "#", "@", "%", "\\", "/../")) or not (
            self.ref.endswith(f"/{self.sha256}.json")
        ):
            raise ValueError("EOD evidence must identify immutable raw content")
        return self


class EODMemberResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_key: str = Field(min_length=1, max_length=150)
    outcome: Literal["data", "no_data", "invalid", "missing", "mapping_gap"]
    raw: EODRawEvidence | None = None
    reason: Literal["row_structure_invalid", "no_source_row"] | None = None


class EODCompletionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[2]
    schema_id: Literal["market_eod"]
    schema_version: Literal[2]
    trade_date: date
    members: list[EODMemberResult] = Field(min_length=1, max_length=10000)
    item_id: UUID | None = None
    request_id: UUID | None = None

    @model_validator(mode="after")
    def unique_members(self) -> "EODCompletionEvidence":
        keys = [row.member_key for row in self.members]
        if len(set(keys)) != len(keys):
            raise ValueError("EOD completion members must be unique")
        return self
