"""Synchronous completeness and freshness policy for canonical ingress."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from statistics import median
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import CalendarRevisionDay
from app.models.registry import IngestionRun
from app.schemas.ingress import DeliveryMode, IngressRequestV1
from app.services.calendar_management import published_year
from app.services.feed_scope import lock_feed_scope
from app.services.slot_identity import normalize_slot_payload
from app.utils import utc_now
from app.vocabulary import SOURCE_NAME_PATTERN

Ratio = Annotated[float, Field(ge=0, lt=1)]
PolicyCode = Literal[
    "BATCH_RECORD_COUNT_DROP",
    "STALE_PAYLOAD",
    "LATEST_DATE_MISSING",
    "CALENDAR_UNAVAILABLE",
]


class PolicyAction(str, Enum):
    DISABLED = "disabled"
    WARN = "warn"
    REJECT = "reject"


class DeliveryExpectationMode(str, Enum):
    """Delivery modes understood by a dataset policy.

    ``sequenced_snapshot`` intentionally lives here rather than in the
    provider-neutral EOD ingress ``DeliveryMode`` enum.  Minute contracts own
    that discriminator while policy evaluation and monitoring need to retain
    the mode in a typed registry projection.
    """

    FULL_SNAPSHOT = "full_snapshot"
    INCREMENTAL = "incremental"
    BACKFILL = "backfill"
    SEQUENCED_SNAPSHOT = "sequenced_snapshot"


# Keep a descriptive alias for callers that use the policy namespace rather
# than the expectation model name.
DeliveryPolicyMode = DeliveryExpectationMode


class BaselinePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A source-specific pilot may opt out of the rolling baseline while still
    # retaining the same typed policy shape as the global dataset policy.
    enabled: bool = True
    strategy: Literal["rolling_median"] = "rolling_median"
    scope: Literal["dataset_source_schema"] = "dataset_source_schema"
    window_size: int = Field(default=7, ge=3, le=31)
    minimum_history: int = Field(default=3, ge=1, le=31)

    @model_validator(mode="after")
    def validate_history(self) -> "BaselinePolicy":
        if self.minimum_history > self.window_size:
            raise ValueError("minimum_history must not exceed window_size")
        return self


class RecordCountPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_record_count: int = Field(default=0, ge=0)
    maximum_count_drop_ratio: Ratio = 0.0
    action: PolicyAction = PolicyAction.WARN


class FreshnessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maximum_fetch_age_hours: float = Field(default=36, gt=0, le=24 * 30)
    allowed_clock_skew_minutes: int = Field(default=5, ge=0, le=60)
    action: PolicyAction = PolicyAction.WARN


class LatestDatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    calendar_market: str = Field(min_length=1, max_length=10)
    timezone: str = Field(min_length=1, max_length=100)
    market_close_time: time
    availability_grace_minutes: int = Field(default=120, ge=0, le=24 * 60)
    action: PolicyAction = PolicyAction.WARN

    @field_validator("calendar_market")
    @classmethod
    def normalize_market(cls, value: str) -> str:
        return value.upper()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class MissingDeliveryPolicy(BaseModel):
    """Opt-in monitor configuration for expected canonical feed sources."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["disabled", "warn"] = "disabled"
    expected_sources: list[str] = Field(
        default_factory=list,
        max_length=32,
    )
    # The monitor deadline is intentionally independent from the ingest
    # latest-date availability grace.  It is interpreted in the timezone of
    # ``LatestDatePolicy`` and is only used by delivery monitoring and the
    # market-freshness projection.  ``None`` preserves the legacy behavior
    # for policies created before this field existed.
    deadline_local_time: time | None = None

    @field_validator("expected_sources")
    @classmethod
    def normalize_sources(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(
            not value or len(value) > 50 or re.fullmatch(SOURCE_NAME_PATTERN, value) is None
            for value in normalized
        ):
            raise ValueError("expected_sources must contain stable lowercase provider names")
        if len(normalized) != len(set(normalized)):
            raise ValueError("expected_sources must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def require_enabled_sources(self) -> "MissingDeliveryPolicy":
        if self.action == "warn" and not self.expected_sources:
            raise ValueError("expected_sources must be non-empty when missing delivery is enabled")
        return self


class SourcePolicyOverride(BaseModel):
    """Typed source/provider-scoped delivery policy overrides.

    Every section is optional and inherits the dataset-wide policy when it is
    omitted.  ``baseline.enabled=False`` is the explicit, bounded way to
    bypass historical rolling-baseline comparisons for a pilot feed.
    """

    model_config = ConfigDict(extra="forbid")

    baseline: BaselinePolicy | None = None
    record_count: RecordCountPolicy | None = None
    freshness: FreshnessPolicy | None = None
    latest_date: LatestDatePolicy | None = None


class FreshnessSchedule(BaseModel):
    """Operator-owned schedule used to group expected delivery feeds."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    slot_id: Literal[
        "western_markets_window",
        "global_markets_window",
        "taiwan_market_window",
        "asia_pacific_markets_window",
    ]
    local_time: time
    timezone: Literal["Asia/Taipei"]
    target_date_lag_days: int = Field(default=0, ge=0, le=366)
    expected_sources: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_slot_payload(cls, value: Any) -> Any:
        # A legacy schedule may still arrive from an operator-owned JSONB
        # config during rollout.  Normalize its ID and encoded clock before
        # Literal validation; all model dumps are canonical thereafter.
        return normalize_slot_payload(value)

    @field_validator("expected_sources")
    @classmethod
    def normalize_sources(cls, values: list[str]) -> list[str]:
        return MissingDeliveryPolicy.normalize_sources(values)

    @model_validator(mode="after")
    def require_enabled_sources(self) -> "FreshnessSchedule":
        if self.enabled and not self.expected_sources:
            raise ValueError("expected_sources must be non-empty when schedule is enabled")
        return self


class DeliveryExpectation(BaseModel):
    """Typed dataset delivery policy with compatibility for the original flat config."""

    # Top-level extras are ignored for compatibility with operator annotations
    # in the previously untyped JSONB object.
    model_config = ConfigDict(extra="ignore")

    delivery_mode: DeliveryExpectationMode = DeliveryExpectationMode.FULL_SNAPSHOT
    baseline: BaselinePolicy = Field(default_factory=BaselinePolicy)
    record_count: RecordCountPolicy = Field(default_factory=RecordCountPolicy)
    freshness: FreshnessPolicy = Field(default_factory=FreshnessPolicy)
    latest_date: LatestDatePolicy | None = None
    missing_delivery: MissingDeliveryPolicy = Field(default_factory=MissingDeliveryPolicy)
    schedule: FreshnessSchedule | None = None
    source_overrides: dict[str, SourcePolicyOverride] = Field(
        default_factory=dict,
        validation_alias=AliasChoices(
            "source_overrides",
            "provider_overrides",
            "source_policy_overrides",
            "provider_policy_overrides",
        ),
        serialization_alias="source_overrides",
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_flat_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        record = dict(migrated.get("record_count") or {})
        freshness = dict(migrated.get("freshness") or {})
        if "minimum_record_count" in migrated:
            record["minimum_record_count"] = migrated.pop("minimum_record_count")
        if "maximum_count_drop_ratio" in migrated:
            record["maximum_count_drop_ratio"] = migrated.pop("maximum_count_drop_ratio")
        if "freshness_hours" in migrated:
            freshness["maximum_fetch_age_hours"] = migrated.pop("freshness_hours")
        if record:
            migrated["record_count"] = record
        if freshness:
            migrated["freshness"] = freshness
        return migrated

    @field_validator("source_overrides", mode="before")
    @classmethod
    def normalize_source_overrides(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("source_overrides must be an object keyed by source")
        normalized: dict[str, Any] = {}
        for source, override in value.items():
            if not isinstance(source, str):
                raise ValueError("source_overrides keys must be stable lowercase source names")
            key = source.strip().lower()
            if not key or re.fullmatch(SOURCE_NAME_PATTERN, key) is None:
                raise ValueError("source_overrides keys must be stable lowercase source names")
            if key in normalized:
                raise ValueError("source_overrides keys must be unique after normalization")
            normalized[key] = override
        return normalized

    @model_validator(mode="after")
    def normalize_provider_alias(self) -> "DeliveryExpectation":
        # ``provider_overrides`` is accepted as a backwards-compatible input
        # alias via ``validation_alias``.  Keep the canonical field stable for
        # deterministic policy serialization and audit evidence.
        self.source_overrides = {
            key.strip().lower(): value for key, value in self.source_overrides.items()
        }
        return self

    def for_source(self, source: str | None) -> "DeliveryExpectation":
        """Return an effective policy with a source-scoped override applied."""
        key = source.strip().lower() if isinstance(source, str) else ""
        override = self.source_overrides.get(key)
        if override is None:
            return self
        values = self.model_dump(mode="python", by_alias=False)
        for section in ("baseline", "record_count", "freshness", "latest_date"):
            value = getattr(override, section)
            if value is not None:
                values[section] = value.model_dump(mode="python")
        # Preserve the source map in the shaped result; this is useful for
        # audit serialization while evaluation itself only consumes sections.
        return type(self).model_validate(values)

    @property
    def provider_overrides(self) -> dict[str, SourcePolicyOverride]:
        """Compatibility name for callers that call providers instead of sources."""
        return self.source_overrides


class PolicyViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: PolicyCode
    action: Literal["warn", "reject"]
    reason: str = Field(max_length=50)
    observed: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)


class DeliveryPolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["pass", "warn", "reject"]
    primary_code: str | None = None
    violations: list[PolicyViolation] = Field(default_factory=list, max_length=8)
    baseline_status: Literal["disabled", "cold_start", "active", "not_applicable"]
    baseline_run_ids: list[UUID] = Field(default_factory=list, max_length=31)
    baseline_counts: list[int] = Field(default_factory=list, max_length=31)
    baseline_median: float | None = None
    effective_count_threshold: int | None = None
    evaluated_at: datetime

    def bounded_details(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class DeliveryPolicyRejectedError(ValueError):
    def __init__(self, result: DeliveryPolicyResult):
        self.result = result
        super().__init__(result.primary_code or "Delivery rejected by dataset policy")


_PRIMARY_PRIORITY = {
    "LATEST_DATE_MISSING": 0,
    "STALE_PAYLOAD": 1,
    "BATCH_RECORD_COUNT_DROP": 2,
    "CALENDAR_UNAVAILABLE": 3,
}


def parse_delivery_expectation(config: dict | None) -> DeliveryExpectation | None:
    if not isinstance(config, dict):
        return None
    raw = config.get("delivery_expectation")
    if raw is None:
        return None
    return DeliveryExpectation.model_validate(raw)


async def lock_delivery_policy_scope(
    db: AsyncSession,
    request: IngressRequestV1,
) -> None:
    """Serialize duplicate recheck, baseline evaluation, and delivery creation."""
    await lock_feed_scope(
        db,
        dataset_key=request.dataset_key,
        source=request.source,
        schema_id=request.schema_id,
        schema_version=request.schema_version,
        wait=True,
    )


async def _baseline_runs(
    db: AsyncSession,
    request: IngressRequestV1,
    policy: BaselinePolicy,
) -> list[IngestionRun]:
    data_date = request.payload.batch.data_date
    statement = (
        select(IngestionRun)
        .where(
            IngestionRun.dataset_key == request.dataset_key,
            IngestionRun.source == request.source,
            IngestionRun.schema_id == request.schema_id,
            IngestionRun.schema_version == request.schema_version,
            IngestionRun.status == "completed",
            IngestionRun.policy_outcome == "pass",
            IngestionRun.delivery_mode == DeliveryMode.FULL_SNAPSHOT.value,
            IngestionRun.is_rerun.is_(False),
            IngestionRun.batch_data_date < data_date,
        )
        .distinct(IngestionRun.batch_data_date)
        .order_by(IngestionRun.batch_data_date.desc(), IngestionRun.created_at.desc())
        .limit(policy.window_size)
    )
    return list((await db.execute(statement)).scalars().all())


async def resolve_expected_data_date(
    db: AsyncSession,
    policy: LatestDatePolicy,
    now: datetime,
    *,
    strict_current_session: bool = False,
    current_session_deadline: time | None = None,
    operational_deadline_timezone: str | None = None,
    target_date_lag_days: int = 0,
) -> tuple[date | None, str | None]:
    """Resolve the latest calendar date currently expected for a feed.

    ``availability_grace_minutes`` remains the authoritative ingest-time
    latest-date cutoff.  Callers that need a later operational monitoring
    deadline may supply ``current_session_deadline`` explicitly.  When an
    ``operational_deadline_timezone`` and ``target_date_lag_days`` are also
    supplied, the current open session cutoff is evaluated at
    ``trade_date + target_date_lag_days`` in that operational timezone.  When
    omitted, the historical close-plus-grace behavior is unchanged.
    """
    if not 0 <= target_date_lag_days <= 366:
        raise ValueError("target_date_lag_days must be between 0 and 366")
    zone = ZoneInfo(policy.timezone)
    local_now = now.astimezone(zone)
    today = local_now.date()
    current = await published_year(db, policy.calendar_market, today.year)
    if current is None:
        return None, "calendar_does_not_cover_evaluation_date"
    rows: list[CalendarRevisionDay] = [day for day in current[2] if day.trade_date <= today]
    year = today.year - 1
    rows.sort(key=lambda row: row.trade_date, reverse=True)
    if not rows or rows[0].trade_date != today:
        return None, "calendar_does_not_cover_evaluation_date"
    if strict_current_session and rows[0].is_open:
        close_at = rows[0].session_close or policy.market_close_time
        if current_session_deadline is None:
            cutoff = datetime.combine(rows[0].trade_date, close_at, tzinfo=zone) + timedelta(
                minutes=policy.availability_grace_minutes
            )
            comparison_now = local_now
        else:
            deadline_zone = ZoneInfo(operational_deadline_timezone or policy.timezone)
            cutoff_date = rows[0].trade_date + timedelta(days=target_date_lag_days)
            cutoff = datetime.combine(
                cutoff_date,
                current_session_deadline,
                tzinfo=deadline_zone,
            )
            comparison_now = now.astimezone(deadline_zone)
        if comparison_now < cutoff:
            # Before today's current session is due, retain the normal
            # resolver semantics and evaluate the most recent prior open
            # session instead of suppressing its expectation entirely.
            rows = rows[1:]
    while True:
        for row in rows[:32]:
            if not row.is_open:
                continue
            close_at = row.session_close or policy.market_close_time
            cutoff = datetime.combine(row.trade_date, close_at, tzinfo=zone) + timedelta(
                minutes=policy.availability_grace_minutes
            )
            if local_now >= cutoff:
                return row.trade_date, None
        if len(rows) >= 32:
            return None, "no_closed_open_session_in_calendar_window"
        previous = await published_year(db, policy.calendar_market, year)
        if previous is None:
            return None, "calendar_does_not_cover_evaluation_date"
        rows.extend(previous[2])
        rows.sort(key=lambda row: row.trade_date, reverse=True)
        year -= 1


def _violation(
    *,
    code: PolicyCode,
    action: PolicyAction,
    reason: str,
    observed: dict[str, Any],
    expected: dict[str, Any],
) -> PolicyViolation | None:
    if action == PolicyAction.DISABLED:
        return None
    return PolicyViolation(
        code=code,
        action=action.value,
        reason=reason,
        observed=observed,
        expected=expected,
    )


async def evaluate_delivery_policy(
    db: AsyncSession,
    request: IngressRequestV1,
    expectation: DeliveryExpectation | None,
    *,
    now: datetime | None = None,
) -> DeliveryPolicyResult:
    evaluated_at = (now or utc_now()).astimezone(timezone.utc)
    mode = request.payload.batch.delivery_mode
    mode_value = getattr(mode, "value", mode)
    if expectation is None:
        return DeliveryPolicyResult(
            outcome="pass",
            baseline_status="disabled",
            evaluated_at=evaluated_at,
        )

    # Apply a stable source/provider-specific override without mutating the
    # registry object.  The global policy remains the default for every source
    # that has no explicit override.
    expectation = expectation.for_source(request.source)

    violations: list[PolicyViolation] = []
    baseline_status: Literal["disabled", "cold_start", "active", "not_applicable"] = (
        "not_applicable"
    )
    baseline_runs: list[IngestionRun] = []
    baseline_value: float | None = None
    effective_threshold: int | None = None

    if mode_value == DeliveryMode.FULL_SNAPSHOT.value:
        count_policy = expectation.record_count
        effective_threshold = count_policy.minimum_record_count
        if count_policy.action == PolicyAction.DISABLED:
            baseline_status = "disabled"
        elif not expectation.baseline.enabled:
            # A pilot can bypass historical rolling comparisons while keeping
            # its explicit absolute minimum record count.
            baseline_status = "disabled"
            observed_count = len(request.payload.data)
            if observed_count < effective_threshold:
                item = _violation(
                    code="BATCH_RECORD_COUNT_DROP",
                    action=count_policy.action,
                    reason="absolute_minimum",
                    observed={"record_count": observed_count},
                    expected={"minimum_record_count": effective_threshold},
                )
                if item:
                    violations.append(item)
        else:
            baseline_runs = await _baseline_runs(db, request, expectation.baseline)
            observed_count = len(request.payload.data)
            if len(baseline_runs) >= expectation.baseline.minimum_history:
                baseline_status = "active"
                baseline_value = float(median([run.raw_records for run in baseline_runs]))
                relative_threshold = math.ceil(
                    baseline_value * (1 - count_policy.maximum_count_drop_ratio)
                )
                effective_threshold = max(effective_threshold, relative_threshold)
            else:
                baseline_status = "cold_start"
            if observed_count < effective_threshold:
                reason = (
                    "absolute_minimum"
                    if observed_count < count_policy.minimum_record_count
                    else "relative_drop"
                )
                item = _violation(
                    code="BATCH_RECORD_COUNT_DROP",
                    action=count_policy.action,
                    reason=reason,
                    observed={"record_count": observed_count},
                    expected={"minimum_record_count": effective_threshold},
                )
                if item:
                    violations.append(item)

    if mode_value in {
        DeliveryMode.FULL_SNAPSHOT.value,
        DeliveryMode.INCREMENTAL.value,
        DeliveryExpectationMode.SEQUENCED_SNAPSHOT.value,
    }:
        freshness = expectation.freshness
        age = evaluated_at - request.fetched_at
        if age > timedelta(hours=freshness.maximum_fetch_age_hours):
            item = _violation(
                code="STALE_PAYLOAD",
                action=freshness.action,
                reason="stale",
                observed={"fetched_at": request.fetched_at.isoformat()},
                expected={"maximum_fetch_age_hours": freshness.maximum_fetch_age_hours},
            )
            if item:
                violations.append(item)
        elif age < -timedelta(minutes=freshness.allowed_clock_skew_minutes):
            item = _violation(
                code="STALE_PAYLOAD",
                action=freshness.action,
                reason="future_clock",
                observed={"fetched_at": request.fetched_at.isoformat()},
                expected={"allowed_clock_skew_minutes": freshness.allowed_clock_skew_minutes},
            )
            if item:
                violations.append(item)

    latest = expectation.latest_date
    if (
        mode_value
        in {
            DeliveryMode.FULL_SNAPSHOT.value,
            DeliveryExpectationMode.SEQUENCED_SNAPSHOT.value,
        }
        and latest is None
    ):
        violations.append(
            PolicyViolation(
                code="CALENDAR_UNAVAILABLE",
                action="warn",
                reason="latest_date_policy_not_configured",
                observed={"evaluation_date": evaluated_at.date().isoformat()},
                expected={},
            )
        )
    elif (
        mode_value
        in {
            DeliveryMode.FULL_SNAPSHOT.value,
            DeliveryExpectationMode.SEQUENCED_SNAPSHOT.value,
        }
        and latest is not None
        and latest.action != PolicyAction.DISABLED
    ):
        expected_date, unavailable_reason = await resolve_expected_data_date(
            db,
            latest,
            evaluated_at,
            strict_current_session=mode_value == DeliveryExpectationMode.SEQUENCED_SNAPSHOT.value,
        )
        if expected_date is None:
            if unavailable_reason != "delivery_not_due":
                violations.append(
                    PolicyViolation(
                        code="CALENDAR_UNAVAILABLE",
                        action="warn",
                        reason=unavailable_reason or "calendar_unavailable",
                        observed={"evaluation_date": evaluated_at.date().isoformat()},
                        expected={"calendar_market": latest.calendar_market},
                    )
                )
        else:
            row_dates = [row.trade_date for row in request.payload.data]
            maximum_row_date = max(row_dates) if row_dates else None
            if (
                request.payload.batch.data_date != expected_date
                or maximum_row_date != expected_date
            ):
                item = _violation(
                    code="LATEST_DATE_MISSING",
                    action=latest.action,
                    reason="expected_date_not_covered",
                    observed={
                        "batch_data_date": request.payload.batch.data_date.isoformat(),
                        "maximum_row_date": (
                            maximum_row_date.isoformat() if maximum_row_date else None
                        ),
                    },
                    expected={"data_date": expected_date.isoformat()},
                )
                if item:
                    violations.append(item)

    rejecting = [item for item in violations if item.action == "reject"]
    outcome: Literal["pass", "warn", "reject"] = (
        "reject" if rejecting else "warn" if violations else "pass"
    )
    candidates = rejecting or violations
    primary = (
        min(candidates, key=lambda item: _PRIMARY_PRIORITY[item.code]).code if candidates else None
    )
    return DeliveryPolicyResult(
        outcome=outcome,
        primary_code=primary,
        violations=violations,
        baseline_status=baseline_status,
        baseline_run_ids=[run.run_id for run in baseline_runs],
        baseline_counts=[run.raw_records for run in baseline_runs],
        baseline_median=baseline_value,
        effective_count_threshold=effective_threshold,
        evaluated_at=evaluated_at,
    )
