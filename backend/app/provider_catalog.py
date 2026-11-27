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
# A provider's execution lane: its own FIFO, owner and consumer. Every provider keeps
# the default lane; historical work always runs there.
DEFAULT_LANE = "default"
LaneName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,29}$")]
EnvName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)]
Clock = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
PositiveInt = Annotated[int, Field(gt=0)]
# A feed may reach further back than the production default of 366 days (the
# latest year), never less, and at most about ten years.
HistoricalLookbackDays = Annotated[int, Field(ge=367, le=3660)]
DayOffset = Annotated[int, Field(ge=0, le=366)]
ConsumerRole = Literal["full_market", "historical"]
# Every acquiring feed is due this long after its scheduled start, in the schedule's timezone.
COMPLETION_DEADLINE_HOURS_AFTER_SCHEDULE = 12
# The last publication retry comes this long before the deadline, as the last
# no-data round does (Fetcher ``FULL_WORK_NO_DATA_FINAL_ROUND_LEAD_SECONDS``),
# leaving the final deliveries time to normalize before the date is judged.
PUBLICATION_FINAL_ROUND_LEAD_SECONDS = 1800
# A date not yet published keeps being retried until then: one window, derived
# from the deadline, never configured per feed.
PUBLICATION_RETRY_WINDOW_SECONDS = (
    COMPLETION_DEADLINE_HOURS_AFTER_SCHEDULE * 3600 - PUBLICATION_FINAL_ROUND_LEAD_SECONDS
)


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


def completion_deadline_for(schedule: AcquisitionSchedule) -> CompletionDeadline:
    """The one deadline rule: a fixed wall-clock span after the scheduled start."""
    hour, minute = map(int, schedule.local_time.split(":"))
    days, hour = divmod(hour + COMPLETION_DEADLINE_HOURS_AFTER_SCHEDULE, 24)
    return CompletionDeadline(
        timezone=schedule.timezone,
        local_time=f"{hour:02d}:{minute:02d}",
        trade_date_day_offset=schedule.target_date_lag_days + days,
    )


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


# How a historical date's members are frozen: ``dated_release`` binds each date to
# the Full release that applied to it; ``current_release`` binds the whole request
# to the release current at creation (the provider can only list today's
# members); ``daily_report`` reads members from the date's own report.
HistoricalCohort = Literal["dated_release", "current_release", "daily_report"]
# Provider requests one historical date costs, for operator estimates: a fixed
# count of shared reports/tables, or one request per frozen member.
HistoricalRequests = PositiveInt | Literal["per_member"]
# Where a trade date's universe comes from: ``refreshed`` lists are refreshed by
# maintenance and a plan freezes the latest published release; a ``dated`` list
# is built from the date's own provider observation and a plan needs that date's.
UniverseMode = Literal["refreshed", "dated"]
# Whether the Fetcher also delivers rows the provider served for symbols outside
# the frozen list. Only a provider response covering the whole market can serve
# them; ``none`` keeps every delivered row inside the frozen members.
UnlistedSymbols = Literal["deliver", "none"]


class FeedDefinition(CatalogModel):
    provider: Identifier
    dataset_key: Identifier
    lane: LaneName
    market: Literal["US", "HK", "TW"]
    calendar_market: Literal["US", "HK", "TW", "TAIFEX"]
    asset_class: Literal["equity", "etf", "future", "index"]
    # Granularity of the delivered records: a daily row, a one-minute bar or a trade.
    frequency: Literal["daily", "minute", "tick"]
    contract: ContractDefinition
    lifecycle: Literal["active", "draining", "retired"]
    capabilities: FeedCapabilities
    schedule: AcquisitionSchedule | None
    completion_deadline: CompletionDeadline | None
    bounded_monitor: BoundedMonitor
    delivery: DeliveryCompatibility
    historical_cohort: HistoricalCohort | None
    historical_requests_per_date: HistoricalRequests | None
    # Production backfill window in calendar days; None keeps the 366-day default.
    # Staging stays at its own short window regardless.
    historical_lookback_days: HistoricalLookbackDays | None
    universe_mode: UniverseMode
    unlisted_symbols: UnlistedSymbols
    # A weekday default never stands in for a missing exchange calendar: the date waits.
    calendar_required: bool

    @property
    def publication_retry_window_seconds(self) -> int | None:
        """Seconds after the scheduled start a not-yet-published date is retried."""
        return PUBLICATION_RETRY_WINDOW_SECONDS if self.capabilities.acquisition else None

    @model_validator(mode="after")
    def coherent_capabilities(self) -> Self:
        caps = self.capabilities
        if (self.contract.schema_id == "intraday_partition") != (self.frequency != "daily"):
            raise ValueError("only intraday_partition feeds deliver minute or tick records")
        if caps.acquisition:
            if self.lifecycle != "active" or not caps.source:
                raise ValueError("acquisition requires active Source capability")
            if self.schedule is None or self.completion_deadline is None:
                raise ValueError("acquiring feed requires schedule and completion deadline")
            if self.completion_deadline != completion_deadline_for(self.schedule):
                raise ValueError(
                    "completion deadline must be "
                    f"{COMPLETION_DEADLINE_HOURS_AFTER_SCHEDULE} hours after the scheduled start"
                )
        elif self.schedule is not None:
            raise ValueError("non-acquiring feed must not publish a schedule")
        if caps.historical != "unsupported" and (not caps.source or self.lifecycle != "active"):
            raise ValueError("historical work requires an active Source capability")
        if (self.historical_cohort is None) != (caps.historical == "unsupported"):
            raise ValueError("historical feeds, and only they, declare a historical cohort")
        if (self.historical_requests_per_date is None) != (caps.historical == "unsupported"):
            raise ValueError("historical feeds, and only they, declare requests per date")
        if self.historical_lookback_days is not None and caps.historical == "unsupported":
            raise ValueError("only historical feeds may extend the backfill window")
        if (
            self.historical_requests_per_date == "per_member"
            and self.historical_cohort != "dated_release"
        ):
            raise ValueError("per-member estimates need a dated_release member list")
        if self.unlisted_symbols == "deliver" and (
            self.contract.schema_id != "market_eod" or self.universe_mode != "refreshed"
        ):
            # A dated universe is the date's own report, so nothing it served is unlisted.
            raise ValueError("only refreshed market_eod feeds deliver unlisted symbols")
        if self.lifecycle == "retired" and (caps.source or self.bounded_monitor.enabled):
            raise ValueError("retired feed cannot accept Source writes or monitor new deliveries")
        if self.lane != DEFAULT_LANE and (not caps.acquisition or caps.historical != "unsupported"):
            # Historical, maintenance of retired feeds and reruns of non-acquiring feeds
            # all run in the default lane; only a daily-acquiring feed may leave it.
            raise ValueError(
                "only acquiring feeds without historical capability leave the default lane"
            )
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
        calendars: dict[str, str] = {}
        for feed in self.feeds:
            if calendars.setdefault(feed.dataset_key, feed.calendar_market) != (
                feed.calendar_market
            ):
                raise ValueError("feeds of one dataset must share a calendar market")
            pair = (feed.provider, feed.dataset_key)
            if pair in pairs:
                raise ValueError("duplicate provider/dataset feed")
            pairs.add(pair)
            if feed.provider not in providers:
                raise ValueError("unknown feed provider reference")
            roles = providers[feed.provider].consumer_roles
            if feed.capabilities.acquisition and "full_market" not in roles:
                raise ValueError("acquiring feed requires daily consumers")
            if feed.capabilities.historical != "unsupported" and "historical" not in roles:
                raise ValueError("historical capability requires a consumer")
        for name in providers:
            lanes = {feed.lane for feed in self.feeds if feed.provider == name}
            if lanes - {DEFAULT_LANE} and not any(
                feed.provider == name
                and feed.lane == DEFAULT_LANE
                and feed.capabilities.acquisition
                for feed in self.feeds
            ):
                # The default lane's consumer is the provider's only historical and
                # maintenance runner; it must have an acquiring feed of its own.
                raise ValueError("a provider with lanes needs an acquiring default-lane feed")
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
