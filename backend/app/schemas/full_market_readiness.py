"""Management declarations and Source acknowledgements, with bounded capability inputs."""

import math
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.full_market import FullMarketModel, _safe_evidence_url
from app.utils import ensure_utc


class AccountAllocation(FullMarketModel):
    account_id: str = Field(min_length=1, max_length=100)
    environment_exclusive: bool
    environment_allocations: dict[str, dict[str, int | float]] = Field(default_factory=dict)
    provider_totals: dict[str, int | float] = Field(default_factory=dict)
    governor_identity: str = Field(min_length=1, max_length=200)
    consumers: list[str] = Field(min_length=2, max_length=20)
    # Worst-case bounded + historical request allowance before full-market work.
    usage_day: date
    used_requests: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    other_requests_per_day: int = Field(ge=0)
    other_bytes_per_day: int = Field(ge=0)


class ReadinessDeclaration(FullMarketModel):
    version: Literal[1] = 1
    provider: Literal["twelve_data", "finlab", "shioaji", "taifex"]
    status: Literal["verified"]
    environment: Literal["local", "staging", "production"]
    source_client_id: UUID
    runtime_id: str = Field(min_length=1, max_length=200)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_allocation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    datasets: list[str] = Field(min_length=1, max_length=7)
    verified_at: datetime
    expires_at: datetime
    evidence_url: str = Field(max_length=2048)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requests_per_day: int = Field(gt=0)
    requests_per_minute: int = Field(gt=0)
    bytes_per_day: int = Field(gt=0)
    max_response_bytes: int = Field(gt=0, le=2 * 1024**3)
    requests_per_second: float = Field(gt=0, allow_inf_nan=False)
    provider_call_seconds: float = Field(gt=0, allow_inf_nan=False)
    source_requests_per_minute: int = Field(default=120, gt=0)
    source_check_seconds: float = Field(gt=0, allow_inf_nan=False)
    completion_window_seconds: int = Field(gt=0, le=86400)
    account: AccountAllocation

    @field_validator("evidence_url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        return _safe_evidence_url(value)

    @field_validator("verified_at", "expires_at")
    @classmethod
    def timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Readiness timestamps must include timezone")
        return ensure_utc(value)

    @model_validator(mode="after")
    def consistent(self) -> "ReadinessDeclaration":
        from app.services.source_clients import PROVIDER_DATASET_SCOPE

        if len(set(self.datasets)) != len(self.datasets) or not set(self.datasets) <= set(
            PROVIDER_DATASET_SCOPE[self.provider]
        ):
            raise ValueError("Readiness scope is invalid")
        if self.provider != "finlab" and self.max_response_bytes > 16 * 1024**2:
            raise ValueError("HTTP provider response bound exceeds supported ceiling")
        if self.expires_at <= self.verified_at:
            raise ValueError("Readiness expiry must follow verification")
        required = {"full_market"}
        if not required <= set(self.account.consumers):
            raise ValueError("Account governor must cover every installed consumer")
        if not self.account.environment_exclusive:
            allocations = self.account.environment_allocations
            if self.environment not in allocations or not set(allocations) <= {
                "local",
                "staging",
                "production",
            }:
                raise ValueError("Explicit environment allocations required")
            for field in (
                "requests_per_day",
                "bytes_per_day",
                "requests_per_minute",
                "requests_per_second",
            ):
                total = self.account.provider_totals.get(field, 0)
                values = [allocation.get(field, 0) for allocation in allocations.values()]
                if (
                    any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or value <= 0
                        for value in values
                    )
                    or not math.isfinite(total)
                    or total <= 0
                    or sum(values) > total
                    or allocations[self.environment].get(field) != getattr(self, field)
                ):
                    raise ValueError("Environment allocations must fit verified account totals")
        from app.services.full_market_admission import declaration_digest

        allocation = {
            key: self.model_dump(mode="json")[key]
            for key in (
                "provider",
                "environment",
                "requests_per_day",
                "requests_per_minute",
                "requests_per_second",
                "bytes_per_day",
                "max_response_bytes",
                "source_requests_per_minute",
                "account",
            )
        }
        digest = declaration_digest(allocation)
        if self.account_allocation_sha256 is not None and self.account_allocation_sha256 != digest:
            raise ValueError("Installed account allocation digest differs from quotas")
        self.account_allocation_sha256 = digest
        return self


class ReadinessReport(FullMarketModel):
    full_market_enabled: bool = True
    enrollment_id: UUID
    declaration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_id: str = Field(min_length=1, max_length=200)
    datasets: list[str] = Field(min_length=1, max_length=7)


class EnrollmentVerification(FullMarketModel):
    """Installed proof identity only; never authorizes provider acquisition."""

    enrollment_id: UUID
    source_client_id: UUID
    declaration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reported_at: datetime
    declaration: ReadinessDeclaration
