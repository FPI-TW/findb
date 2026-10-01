"""Bounded, versioned full-market universe and delivery-plan protocol."""

from datetime import date, datetime, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.ingress import FUTURES_CONTRACT_MONTH_PATTERN


class FullMarketModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _safe_evidence_url(value: str, *, allow_r2: bool = False) -> str:
    parts = urlsplit(value)
    if (
        allow_r2
        and parts.scheme == "r2"
        and parts.hostname
        and not parts.username
        and not parts.password
        and not parts.query
        and not parts.fragment
    ):
        return value
    secret_keys = {
        "api_key",
        "apikey",
        "key",
        "token",
        "secret",
        "password",
        "signature",
        "authorization",
        "access_token",
        "credential",
        "sig",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
        "x-goog-signature",
        "x-goog-credential",
    }
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
        or any(
            key.lower() in secret_keys for key, _ in parse_qsl(parts.query, keep_blank_values=True)
        )
    ):
        raise ValueError("evidence URL must be credential-free HTTPS")
    return value


class EvidenceReference(FullMarketModel):
    url: str = Field(min_length=12, max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _safe_evidence_url(value)


class UniverseMemberInput(FullMarketModel):
    symbol: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9._:-]+$")
    provider_symbol: str = Field(min_length=1, max_length=100)
    exchange: str = Field(min_length=2, max_length=30)
    currency: str = Field(min_length=3, max_length=10, pattern=r"^[A-Z0-9]+$")
    asset_class: Literal["equity", "etf", "future"]
    classification: str | None = Field(default=None, max_length=100)
    contract_code: str | None = Field(default=None, max_length=50)
    product_code: Literal["TX", "MTX", "TMF", "TE", "TF"] | None = None
    contract_month: str | None = Field(
        default=None, max_length=10, pattern=rf"^{FUTURES_CONTRACT_MONTH_PATTERN}$"
    )
    sessions: list[Literal["regular", "after_hours"]] | None = Field(
        default=None, min_length=1, max_length=2
    )
    mapping_status: Literal["mapped", "gap"] = "mapped"
    raw_evidence_ref: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_futures_identity(self) -> "UniverseMemberInput":
        if self.asset_class == "future":
            if (
                not self.product_code
                or not self.contract_code
                or not self.contract_month
                or not self.sessions
            ):
                raise ValueError(
                    "futures require product, contract, month and actual expected sessions"
                )
            if (
                self.symbol != self.product_code
                or self.contract_code != f"{self.product_code}:{self.contract_month}"
            ):
                raise ValueError(
                    "futures symbol and contract identity must preserve TAIFEX product/month"
                )
            if len(set(self.sessions)) != len(self.sessions):
                raise ValueError("futures sessions cannot repeat")
        elif any(
            value is not None
            for value in (self.product_code, self.contract_code, self.contract_month, self.sessions)
        ):
            raise ValueError("non-futures members cannot carry contract identity")
        if self.raw_evidence_ref is not None:
            _safe_evidence_url(self.raw_evidence_ref, allow_r2=True)
        return self


class UniverseSubmitRequest(FullMarketModel):
    version: Literal[1]
    dataset_key: str = Field(pattern=r"^[a-z0-9_]{1,50}$")
    provider: str = Field(pattern=r"^[a-z0-9_]{1,50}$")
    effective_date: date
    observed_at: datetime
    source_timezone: str = Field(min_length=1, max_length=64)
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=20)
    members: list[UniverseMemberInput] = Field(min_length=1, max_length=30000)

    @field_validator("observed_at")
    @classmethod
    def aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class UniverseReleaseResponse(FullMarketModel):
    release_id: UUID
    dataset_key: str
    provider: str
    effective_date: date
    status: Literal["candidate", "published"]
    member_count: int
    member_sha256: str
    change_count: int
    change_ratio: float


class UniverseReleaseDetailResponse(UniverseReleaseResponse):
    members: list[dict]
    evidence: list[dict]


class UniverseListResponse(FullMarketModel):
    data: list[UniverseReleaseResponse]
    published_release_id: UUID | None
    activation: dict


class DeliveryPlanCreateRequest(FullMarketModel):
    version: Literal[1]
    dataset_key: str = Field(pattern=r"^[a-z0-9_]{1,50}$")
    provider: str = Field(pattern=r"^[a-z0-9_]{1,50}$")
    trade_date: date
    release_id: UUID


class DeliveryPartResponse(FullMarketModel):
    part_id: UUID
    work_item_id: str
    member_keys: list[str]
    member_sha256: str


class DeliverySummaryResponse(FullMarketModel):
    expected: int
    data: int
    no_data: int
    missing: int
    blocked: int
    deadline_at: datetime
    is_late: bool
    gaps: list[dict]


class DeliveryPlanResponse(FullMarketModel):
    plan_id: UUID
    dataset_key: str
    provider: str
    trade_date: date
    release_id: UUID
    deadline_at: datetime
    status: Literal["complete", "incomplete"]
    parts: list[DeliveryPartResponse]
    summary: DeliverySummaryResponse


class OutcomeEvidence(FullMarketModel):
    url: str = Field(min_length=12, max_length=2048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    source_symbol: str = Field(min_length=1, max_length=100)
    trade_date: date
    session: Literal["regular", "after_hours"] | None = None
    source_status: str = Field(min_length=1, max_length=100)
    source_excerpt: str = Field(min_length=1, max_length=500)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _safe_evidence_url(value)

    @field_validator("observed_at")
    @classmethod
    def aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class DeliveryOutcomeRequest(FullMarketModel):
    version: Literal[1]
    work_item_id: str = Field(min_length=1, max_length=100)
    member_key: str = Field(min_length=1, max_length=150)
    outcome: Literal["no_data", "blocked"]
    reason: Literal["halted", "no_trade", "mapping_gap", "rate_limited", "source_error"]
    evidence: OutcomeEvidence | None = None

    @model_validator(mode="after")
    def validate_claim(self) -> "DeliveryOutcomeRequest":
        if self.outcome == "no_data":
            if self.reason not in {"halted", "no_trade"} or self.evidence is None:
                raise ValueError("no_data requires halted/no_trade and durable source evidence")
            if self.evidence.source_status != self.reason:
                raise ValueError("no_data evidence source_status must match reason")
        elif self.reason not in {"mapping_gap", "rate_limited", "source_error"}:
            raise ValueError("blocked requires a failure reason")
        return self


class UniversePublishRequest(FullMarketModel):
    evidence_note: str = Field(min_length=10, max_length=2000)
    first_baseline_approved: bool = False
    threshold_exception_approved: bool = False


class FeedActivateRequest(FullMarketModel):
    activation_date: date
    readiness_evidence_note: str = Field(min_length=10, max_length=2000)
    mode: Literal["acceptance", "active"] = "acceptance"


class FeedDeactivateRequest(FullMarketModel):
    evidence_note: str = Field(min_length=10, max_length=2000)
