"""Owner approvals are distinct from fenced Source installation."""

from pydantic import Field, model_validator

from app.schemas.full_market import EvidenceReference, FullMarketModel


class MappingEvidence(FullMarketModel):
    symbol: str = Field(min_length=1, max_length=50)
    provider_symbol: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    exchange: str = Field(min_length=2, max_length=30)
    currency: str = Field(min_length=3, max_length=10)
    reference: EvidenceReference


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
