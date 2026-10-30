"""Owner approvals are distinct from fenced Source installation."""

from typing import Annotated

from pydantic import Field, field_validator, model_validator

from app.schemas.full_market import EvidenceReference, FullMarketModel

ShioajiSymbol = Annotated[
    str, Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9.-]*[A-Za-z0-9][A-Za-z0-9.-]*$")
]


class MappingEvidence(FullMarketModel):
    symbol: ShioajiSymbol
    provider_symbol: ShioajiSymbol
    exchange: str = Field(min_length=2, max_length=30)
    currency: str = Field(min_length=3, max_length=10)
    reference: EvidenceReference

    @field_validator("symbol", "provider_symbol", mode="before")
    @classmethod
    def reject_symbol_whitespace(cls, value):
        # Match Fetcher's _validate_symbol before FullMarketModel strips text.
        if isinstance(value, str) and value != value.strip():
            raise ValueError("Shioaji symbol cannot contain surrounding whitespace")
        return value


class MemberRepairInput(FullMarketModel):
    member_key: str = Field(min_length=1, max_length=150)
    mapping: MappingEvidence | None = None
    evidence: EvidenceReference
    reason: str = Field(min_length=10, max_length=2000)
    observation_limit: int = Field(default=3, ge=1, le=3)


class RepairPreviewRequest(FullMarketModel):
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    members: list[MemberRepairInput] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def unique_members(self):
        if len({row.member_key for row in self.members}) != len(self.members):
            raise ValueError("repair members must be unique")
        return self


class RepairApplyRequest(RepairPreviewRequest):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
