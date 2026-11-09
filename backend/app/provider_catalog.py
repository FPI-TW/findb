"""Validated static provider catalog; runtime authorization stays in the database."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.slot_identity import CanonicalSlotId

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)]
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)]
Clock = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
PositiveInt = Annotated[int, Field(gt=0)]
DayOffset = Annotated[int, Field(ge=0, le=366)]
ConsumerRole = Literal["pilot", "full_market", "historical"]


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SourceCredential(CatalogModel):
    hash_env: EnvName
    name: Annotated[str, Field(min_length=1, max_length=100)]
    description: Annotated[str, Field(min_length=1, max_length=200)]
    rate_limit_requests: PositiveInt
    rate_limit_window_seconds: PositiveInt


class PlatformLimits(CatalogModel):
    requests_per_minute: PositiveInt | None
    max_response_bytes: PositiveInt


class ProviderDefinition(CatalogModel):
    provider: Identifier
    display_name: Annotated[str, Field(min_length=1, max_length=100)]
    container_suffix: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]*$", max_length=100)]
    image_key: Identifier
    consumer_roles: list[ConsumerRole]
    calendar_switch_env: EnvName
    credential_env_names: list[EnvName]
    source_credential: SourceCredential
    platform_limits: PlatformLimits

    @model_validator(mode="after")
    def unique_roles(self) -> Self:
        if len(self.consumer_roles) != len(set(self.consumer_roles)):
            raise ValueError("duplicate consumer role")
        if len(self.credential_env_names) != len(set(self.credential_env_names)):
            raise ValueError("duplicate credential setting")
        return self


class ZonedClock(CatalogModel):
    timezone: str
    local_time: Clock

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class AcquisitionSchedule(ZonedClock):
    target_date_lag_days: DayOffset


class CompletionDeadline(ZonedClock):
    trade_date_day_offset: DayOffset


class BoundedMonitor(CatalogModel):
    enabled: bool
    deadline: CompletionDeadline | None

    @model_validator(mode="after")
    def enabled_deadline(self) -> Self:
        if self.enabled and self.deadline is None:
            raise ValueError("enabled bounded monitoring requires a deadline")
        return self


class ContractDefinition(CatalogModel):
    schema_id: Identifier
    current_version: PositiveInt
    accepted_versions: Annotated[list[PositiveInt], Field(min_length=1)]

    @model_validator(mode="after")
    def current_is_accepted(self) -> Self:
        if len(self.accepted_versions) != len(set(self.accepted_versions)):
            raise ValueError("duplicate accepted contract version")
        if self.current_version not in self.accepted_versions:
            raise ValueError("current contract version must be accepted")
        return self


class FeedCapabilities(CatalogModel):
    source: bool
    acquisition: bool
    historical: Literal["unsupported", "declared", "executable"]


class DeliveryCompatibility(CatalogModel):
    slot_id: CanonicalSlotId


class HistoricalExecution(CatalogModel):
    """Non-schedule inputs retained by the existing historical executors."""

    universe_file: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]+\.json$")]
    outputsize: PositiveInt
    max_attempts: PositiveInt
    retry_base_seconds: PositiveInt
    retry_max_seconds: PositiveInt
    lease_seconds: PositiveInt
    poll_interval_seconds: PositiveInt
    wait_timeout_seconds: PositiveInt
    max_credits_per_run: PositiveInt
    max_records_per_run: PositiveInt

    @model_validator(mode="after")
    def retry_bounds(self) -> Self:
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("historical retry ceiling is below base")
        return self


class HistoricalExecutionProfiles(CatalogModel):
    local: HistoricalExecution
    staging: HistoricalExecution
    production: HistoricalExecution


class FeedDefinition(CatalogModel):
    provider: Identifier
    dataset_key: Identifier
    market: Literal["US", "HK", "TW"]
    calendar_market: Literal["US", "HK", "TW", "TAIFEX"]
    asset_class: Literal["equity", "etf", "future"]
    frequency: Literal["daily"]
    contract: ContractDefinition
    lifecycle: Literal["active", "draining", "retired"]
    capabilities: FeedCapabilities
    schedule: AcquisitionSchedule | None
    completion_deadline: CompletionDeadline | None
    bounded_monitor: BoundedMonitor
    publication_retry_window_seconds: PositiveInt | None
    delivery: DeliveryCompatibility
    historical_execution: HistoricalExecutionProfiles | None

    @model_validator(mode="after")
    def coherent_capabilities(self) -> Self:
        caps = self.capabilities
        if caps.acquisition:
            if self.lifecycle != "active" or not caps.source:
                raise ValueError("acquisition requires active Source capability")
            if self.schedule is None or self.completion_deadline is None:
                raise ValueError("acquiring feed requires schedule and completion deadline")
            if self.publication_retry_window_seconds is None:
                raise ValueError("acquiring feed requires publication retry window")
        elif self.schedule is not None:
            raise ValueError("non-acquiring feed must not publish a schedule")
        if caps.historical != "unsupported" and (not caps.source or self.lifecycle != "active"):
            raise ValueError("historical work requires an active Source capability")
        if self.historical_execution is not None and caps.historical == "unsupported":
            raise ValueError("unsupported historical feed has execution parameters")
        if self.lifecycle == "retired" and (caps.source or self.bounded_monitor.enabled):
            raise ValueError("retired feed cannot accept Source writes or monitor new deliveries")
        return self


class ProviderCatalog(CatalogModel):
    schema_version: Literal[1]
    providers: Annotated[list[ProviderDefinition], Field(min_length=1)]
    feeds: Annotated[list[FeedDefinition], Field(min_length=1)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def references(self) -> Self:
        providers = {p.provider: p for p in self.providers}
        if len(providers) != len(self.providers):
            raise ValueError("duplicate provider")
        for values, label in (
            ([p.container_suffix for p in self.providers], "container suffix"),
            ([p.source_credential.name for p in self.providers], "Source credential name"),
            ([p.source_credential.hash_env for p in self.providers], "Source credential setting"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label}")
        pairs: set[tuple[str, str]] = set()
        for feed in self.feeds:
            pair = (feed.provider, feed.dataset_key)
            if pair in pairs:
                raise ValueError("duplicate provider/dataset feed")
            pairs.add(pair)
            if feed.provider not in providers:
                raise ValueError("unknown feed provider reference")
            roles = providers[feed.provider].consumer_roles
            if feed.capabilities.acquisition and not {"pilot", "full_market"} <= set(roles):
                raise ValueError("acquiring feed requires daily consumers")
            if feed.capabilities.historical != "unsupported" and "historical" not in roles:
                raise ValueError("historical capability requires a consumer")
        for provider in self.providers:
            if provider.image_key not in providers:
                raise ValueError("unknown image provider reference")
            if providers[provider.image_key].image_key != provider.image_key:
                raise ValueError("image reference must point to its owning provider")
            if not any(feed.provider == provider.provider for feed in self.feeds):
                raise ValueError("provider has no feed definitions")
        return self

    def canonical_data(self) -> dict:
        data = self.model_dump(mode="json")
        data["providers"].sort(key=lambda p: p["provider"])
        data["feeds"].sort(key=lambda f: (f["provider"], f["dataset_key"]))
        for provider in data["providers"]:
            provider["consumer_roles"].sort()
            provider["credential_env_names"].sort()
        for feed in data["feeds"]:
            feed["contract"]["accepted_versions"].sort()
        return data

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.canonical_data(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def load_catalog(path: Path, *, contracts_dir: Path) -> ProviderCatalog:
    """Validate structure and resolve all contracts against published schema artifacts."""
    catalog = ProviderCatalog.model_validate(json.loads(path.read_text()))
    manifest = json.loads((contracts_dir / "manifest.json").read_text())
    if manifest.get("manifest_version") != 1:
        raise ValueError("unsupported contract manifest version")
    available = {
        (item["schema_id"], item["schema_version"]): item for item in manifest["contracts"]
    }
    for feed in catalog.feeds:
        for version in feed.contract.accepted_versions:
            pair = (feed.contract.schema_id, version)
            if pair not in available:
                raise ValueError(f"unknown contract reference: {pair}")
            item = available[pair]
            expected_path = f"{pair[0]}/v{version}.schema.json"
            if item["path"] != expected_path:
                raise ValueError("contract manifest path differs from identity")
            raw = (contracts_dir / expected_path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("contract artifact checksum mismatch")
    return catalog
