"""Synchronous completeness and freshness policy for canonical ingress."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from statistics import median
from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import TradingCalendar
from app.models.registry import IngestionRun
from app.schemas.ingress import DeliveryMode, IngressRequestV1
from app.utils import utc_now

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


class BaselinePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class DeliveryExpectation(BaseModel):
    """Typed dataset delivery policy with compatibility for the original flat config."""

    # Top-level extras are ignored for compatibility with operator annotations
    # in the previously untyped JSONB object.
    model_config = ConfigDict(extra="ignore")

    delivery_mode: DeliveryMode = DeliveryMode.FULL_SNAPSHOT
    baseline: BaselinePolicy = Field(default_factory=BaselinePolicy)
    record_count: RecordCountPolicy = Field(default_factory=RecordCountPolicy)
    freshness: FreshnessPolicy = Field(default_factory=FreshnessPolicy)
    latest_date: LatestDatePolicy | None = None

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


async def _lock_scope(db: AsyncSession, request: IngressRequestV1) -> None:
    scope = ":".join(
        (
            request.dataset_key,
            request.source,
            request.schema_id,
            str(request.schema_version),
        )
    )
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": scope},
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


async def _expected_data_date(
    db: AsyncSession,
    policy: LatestDatePolicy,
    now: datetime,
) -> tuple[date | None, str | None]:
    zone = ZoneInfo(policy.timezone)
    local_now = now.astimezone(zone)
    today = local_now.date()
    rows = list(
        (
            await db.execute(
                select(TradingCalendar)
                .where(
                    TradingCalendar.market == policy.calendar_market,
                    TradingCalendar.trade_date <= today,
                )
                .order_by(TradingCalendar.trade_date.desc())
                .limit(32)
            )
        )
        .scalars()
        .all()
    )
    if not rows or rows[0].trade_date != today:
        return None, "calendar_does_not_cover_evaluation_date"
    for row in rows:
        if not row.is_open:
            continue
        close_at = row.session_close or policy.market_close_time
        cutoff = datetime.combine(row.trade_date, close_at, tzinfo=zone) + timedelta(
            minutes=policy.availability_grace_minutes
        )
        if local_now >= cutoff:
            return row.trade_date, None
    return None, "no_closed_open_session_in_calendar_window"


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
    if expectation is None:
        return DeliveryPolicyResult(
            outcome="pass",
            baseline_status="disabled",
            evaluated_at=evaluated_at,
        )

    await _lock_scope(db, request)
    violations: list[PolicyViolation] = []
    baseline_status: Literal["disabled", "cold_start", "active", "not_applicable"] = (
        "not_applicable"
    )
    baseline_runs: list[IngestionRun] = []
    baseline_value: float | None = None
    effective_threshold: int | None = None

    if mode == DeliveryMode.FULL_SNAPSHOT:
        count_policy = expectation.record_count
        if count_policy.action == PolicyAction.DISABLED:
            baseline_status = "disabled"
        else:
            baseline_runs = await _baseline_runs(db, request, expectation.baseline)
            observed_count = len(request.payload.data)
            effective_threshold = count_policy.minimum_record_count
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

    if mode in {DeliveryMode.FULL_SNAPSHOT, DeliveryMode.INCREMENTAL}:
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
    if mode == DeliveryMode.FULL_SNAPSHOT and latest is None:
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
        mode == DeliveryMode.FULL_SNAPSHOT
        and latest is not None
        and latest.action != PolicyAction.DISABLED
    ):
        expected_date, unavailable_reason = await _expected_data_date(db, latest, evaluated_at)
        if expected_date is None:
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
