"""Baseline/calendar admission and authenticated acquisition authorization.

Owner start locks environment, sorted datasets, calendar markets, then control.
Acquisition locks the authenticated Source credential before control. Legacy
capability enrollment/report/repair tools remain independent diagnostics.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.execution_profiles import ExecutionProfile, scheduler_key, scheduler_profile
from app.models.canonical import CalendarMarket, CalendarYearRevision
from app.models.registry import (
    AdminAuditEvent,
    DatasetRegistry,
    FullMarketAdmission,
    FullMarketAdmissionFeed,
    FullMarketDatasetState,
    FullMarketEnrollment,
    FullMarketEnvironment,
    SchedulerControl,
    SourceClient,
    UniverseRelease,
)
from app.schemas.full_market_readiness import (
    EnrollmentVerification,
    ReadinessDeclaration,
    ReadinessReport,
)
from app.services.calendar_management import complete_published_revision_ids
from app.services.source_clients import PROVIDER_DATASET_SCOPE
from app.utils import ensure_utc, utc_now, uuid7


class AdmissionError(ValueError):
    pass


def declaration_digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def market(dataset: DatasetRegistry) -> str:
    return "TAIFEX" if dataset.dataset_key == "tw_futures_eod" else dataset.market


def local_today(dataset: DatasetRegistry) -> date:
    return (
        utc_now()
        .astimezone(
            ZoneInfo(
                {"US": "America/New_York", "HK": "Asia/Hong_Kong"}.get(
                    dataset.market, "Asia/Taipei"
                )
            )
        )
        .date()
    )


async def lock_environment(db: AsyncSession) -> FullMarketEnvironment:
    environment = get_settings().APP_ENVIRONMENT
    if environment not in {"local", "staging", "production"}:
        raise AdmissionError("environment_invalid")
    await db.execute(
        insert(FullMarketEnvironment)
        .values(environment=environment, enabled=False, updated_at=utc_now())
        .on_conflict_do_nothing()
    )
    return (
        await db.scalars(
            select(FullMarketEnvironment)
            .where(FullMarketEnvironment.environment == environment)
            .with_for_update()
        )
    ).one()


async def reconcile_environment(db: AsyncSession, *, actor: str) -> None:
    from app.services.provider_work import lock_provider

    for provider in ("factset", "finlab", "shioaji", "taifex", "twelve_data"):
        await lock_provider(db, provider)
    row = await lock_environment(db)
    enabled = get_settings().FULL_MARKET_ENABLED
    row.enabled = enabled
    row.updated_at = utc_now()
    if not enabled:
        controls = (
            await db.scalars(
                select(SchedulerControl)
                .where(SchedulerControl.scheduler_key.startswith("full_market_", autoescape=True))
                .order_by(SchedulerControl.scheduler_key)
                .with_for_update()
            )
        ).all()
        for control in controls:
            if control.desired_state == "running":
                control.desired_state = "stopped"
                control.revision += 1
                control.updated_at = utc_now()
                db.add(
                    AdminAuditEvent(
                        actor_type="deployment",
                        actor_id=actor,
                        actor_display_name=actor,
                        action="stop",
                        resource_type="scheduler_control",
                        resource_id=control.scheduler_key,
                        details={
                            "reason": "full_market_flag_disabled",
                            "revision": control.revision,
                        },
                    )
                )
    await db.flush()


async def enroll(
    db: AsyncSession, declaration: ReadinessDeclaration, installation_sha256: str
) -> FullMarketEnrollment:
    await lock_environment(db)
    settings = get_settings()
    now = utc_now()
    if (
        declaration.environment != settings.APP_ENVIRONMENT
        or not declaration.verified_at <= now < declaration.expires_at
    ):
        raise AdmissionError("readiness_environment_or_expiry_invalid")
    client = await db.get(SourceClient, declaration.source_client_id, with_for_update=True)
    if (
        client is None
        or client.revoked_at
        or (client.expires_at and ensure_utc(client.expires_at) <= now)
        or client.source_name != declaration.provider
        or not client.allowed_datasets
        or not set(declaration.datasets) <= set(client.allowed_datasets)
    ):
        raise AdmissionError("readiness_source_binding_invalid")
    payload = declaration.model_dump(mode="json")
    digest = declaration_digest(payload)
    existing = await db.scalar(
        select(FullMarketEnrollment).where(FullMarketEnrollment.declaration_sha256 == digest)
    )
    if existing:
        if existing.installation_sha256 != installation_sha256 or existing.revoked_at:
            raise AdmissionError("readiness_installation_changed")
        return existing
    prior = (
        await db.scalars(
            select(FullMarketEnrollment)
            .where(
                FullMarketEnrollment.environment == settings.APP_ENVIRONMENT,
                FullMarketEnrollment.provider == declaration.provider,
                FullMarketEnrollment.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    for row in prior:
        row.revoked_at = now
    row = FullMarketEnrollment(
        enrollment_id=uuid7(),
        environment=declaration.environment,
        provider=declaration.provider,
        source_client_id=declaration.source_client_id,
        runtime_id=declaration.runtime_id,
        declaration_sha256=digest,
        declaration=payload,
        installation_sha256=installation_sha256,
        expires_at=declaration.expires_at,
        created_at=now,
    )
    db.add(row)
    db.add(
        AdminAuditEvent(
            actor_type="deployment",
            actor_id="full-market-enrollment",
            actor_display_name="Trusted runtime management",
            action="enroll",
            resource_type="full_market_enrollment",
            resource_id=str(row.enrollment_id),
            details={
                "declaration_sha256": digest,
                "installation_sha256": installation_sha256,
                "environment": row.environment,
                "provider": row.provider,
            },
        )
    )
    await db.flush()
    return row


async def report_readiness(db: AsyncSession, body: ReadinessReport) -> dict:
    row = await db.get(FullMarketEnrollment, body.enrollment_id, with_for_update=True)
    if (
        row is None
        or row.revoked_at
        or row.environment != get_settings().APP_ENVIRONMENT
        or row.source_client_id != db.info.get("source_client_id")
        or row.provider != db.info.get("source_name")
        or row.runtime_id != body.runtime_id
        or row.declaration_sha256 != body.declaration_sha256
        or set(body.datasets) != set(row.declaration["datasets"])
        or not set(body.datasets) <= set(db.info.get("allowed_datasets") or [])
        or ensure_utc(row.expires_at) <= utc_now()
    ):
        raise AdmissionError("readiness_report_binding_invalid")
    row.reported_enabled = body.full_market_enabled
    row.reported_at = utc_now()
    await db.commit()
    return {
        "enrollment_id": str(row.enrollment_id),
        "declaration_sha256": row.declaration_sha256,
        "expires_at": row.expires_at.isoformat(),
    }


async def current_enrollment(db: AsyncSession, provider: str) -> FullMarketEnrollment | None:
    return await db.scalar(
        select(FullMarketEnrollment)
        .where(
            FullMarketEnrollment.provider == provider,
            FullMarketEnrollment.environment == get_settings().APP_ENVIRONMENT,
            FullMarketEnrollment.revoked_at.is_(None),
        )
        .order_by(FullMarketEnrollment.created_at.desc())
        .limit(1)
    )


async def verify_enrollment(
    db: AsyncSession,
    *,
    enrollment_id: UUID,
    source_client_id: UUID,
    declaration_sha256: str,
    runtime_id: str,
) -> EnrollmentVerification:
    """Read current management authority and fresh installed ACK, even while stopped."""
    row = await current_enrollment(db, db.info.get("source_name", ""))
    now = utc_now()
    if (
        row is None
        or row.enrollment_id != enrollment_id
        or row.source_client_id != source_client_id
        or source_client_id != db.info.get("source_client_id")
        or row.declaration_sha256 != declaration_sha256
        or row.runtime_id != runtime_id
        or ensure_utc(row.expires_at) <= now
        or row.reported_at is None
        or not 0 <= (now - ensure_utc(row.reported_at)).total_seconds() <= 90
    ):
        raise AdmissionError("current_installed_enrollment_verification_failed")
    try:
        proof = ReadinessDeclaration.model_validate(row.declaration)
    except ValueError:
        raise AdmissionError("current_installed_enrollment_verification_failed") from None
    client = await db.get(SourceClient, source_client_id)
    if (
        client is None
        or client.revoked_at
        or (client.expires_at and ensure_utc(client.expires_at) <= now)
        or client.source_name != row.provider
        or proof.provider != row.provider
        or proof.runtime_id != row.runtime_id
        or proof.source_client_id != source_client_id
        or proof.environment != row.environment
        or proof.expires_at != ensure_utc(row.expires_at)
        or proof.verified_at > now
        or declaration_digest(proof.model_dump(mode="json")) != row.declaration_sha256
        or not set(proof.datasets) <= set(client.allowed_datasets or [])
        or not set(proof.datasets) <= set(db.info.get("allowed_datasets") or [])
    ):
        raise AdmissionError("current_installed_enrollment_verification_failed")
    return EnrollmentVerification(
        enrollment_id=row.enrollment_id,
        source_client_id=source_client_id,
        declaration_sha256=row.declaration_sha256,
        installation_sha256=row.installation_sha256,
        reported_at=row.reported_at,
        declaration=proof,
    )


def capacity_proof(
    proof: ReadinessDeclaration, provider: str, members: dict[str, int]
) -> tuple[dict, list[str]]:
    # Count every ready feed, plus catalogue refresh and worst report fallback.
    calls = sum(
        5 if provider == "finlab" else 7 if provider == "taifex" else count
        for count in members.values()
    )
    calls += {"twelve_data": 4, "finlab": 6, "shioaji": 5, "taifex": 1, "factset": 2}[provider]
    required = math.ceil(calls * 1.2)
    required_bytes = required * proof.max_response_bytes
    rate = min(proof.requests_per_second, proof.requests_per_minute / 60)
    source_checks = 8
    # Leave 20% of the Source key budget for delivery, reports and polling.
    interval = max(1 / rate, 60 * source_checks / (0.8 * proof.source_requests_per_minute))
    source_requests = required * source_checks
    # Every member requires control/auth, delivery/outcome and terminal status,
    # even when its provider response is cached or shared with other members.
    member_source_requests = math.ceil(sum(members.values()) * 4 * 1.2)
    source_requests += member_source_requests
    source_requests += proof.account.other_requests_per_day * source_checks
    seconds = (required + proof.account.other_requests_per_day) * (
        interval + proof.provider_call_seconds
    ) + source_requests * (
        proof.source_check_seconds + 60 / (0.8 * proof.source_requests_per_minute)
    )
    windows = {"us_equity_eod": 10800, "hk_equity_eod": 16200, "tw_futures_eod": 18000}
    completion_window = min(
        proof.completion_window_seconds,
        min(windows.get(dataset, 14400) for dataset in members),
    )
    prior_requests = (
        proof.account.used_requests if proof.account.usage_day == utc_now().date() else 0
    )
    prior_bytes = proof.account.used_bytes if proof.account.usage_day == utc_now().date() else 0
    result = {
        "version": 1,
        "members": members,
        "provider_calls": calls,
        "required_requests": required,
        "required_bytes": required_bytes,
        "required_seconds": seconds,
        "source_checks_per_call": source_checks,
        "effective_acquisition_interval_seconds": interval,
        "completion_window_seconds": completion_window,
        "source_requests": source_requests,
        "member_source_requests": member_source_requests,
        "source_requests_per_member": 4,
        "retry_multiplier": 1.2,
        "other_requests": proof.account.other_requests_per_day,
        "other_bytes": proof.account.other_bytes_per_day,
        "prior_requests": prior_requests,
        "prior_bytes": prior_bytes,
    }
    blockers = []
    if required + proof.account.other_requests_per_day + prior_requests > proof.requests_per_day:
        blockers.append("full_market_request_capacity_insufficient")
    if required_bytes + proof.account.other_bytes_per_day + prior_bytes > proof.bytes_per_day:
        blockers.append("full_market_byte_capacity_insufficient")
    if seconds > completion_window:
        blockers.append("full_market_deadline_capacity_insufficient")
    return result, blockers


async def eligibility(
    db: AsyncSession,
    provider: str,
    datasets: list[DatasetRegistry],
    *,
    profile: ExecutionProfile = "full_market",
) -> dict:
    settings = get_settings()
    blockers = []
    ready = []
    feeds = []
    feed_blockers: set[str] = set()
    for dataset in datasets:
        reasons: list[str] = []
        today = local_today(dataset)
        from app.services.hk_provider_cutover import acquisition_allowed

        if not acquisition_allowed(dataset, provider):
            reasons.append("provider_acquisition_cutover")
        state = await db.get(
            FullMarketDatasetState,
            {"dataset_key": dataset.dataset_key, "execution_profile": profile},
        )
        baseline = await db.scalar(
            select(UniverseRelease)
            .where(
                UniverseRelease.dataset_key == dataset.dataset_key,
                UniverseRelease.provider == provider,
                UniverseRelease.status == "published",
                UniverseRelease.effective_date <= today,
            )
            .order_by(UniverseRelease.effective_date.desc(), UniverseRelease.created_at.desc())
            .limit(1)
        )
        if baseline is None:
            reasons.append("full_market_baseline_missing")
        calendar = await db.scalar(
            select(CalendarYearRevision)
            .where(
                CalendarYearRevision.market == market(dataset),
                CalendarYearRevision.year == today.year,
                CalendarYearRevision.id.in_(complete_published_revision_ids()),
            )
            .limit(1)
        )
        feed_blockers.update(reasons)
        info = {
            "dataset_key": dataset.dataset_key,
            "ready": not reasons,
            "blockers": reasons,
            "first_start_date": state.first_start_date.isoformat() if state else None,
        }
        feeds.append(info)
        if not reasons and baseline:
            ready.append((dataset, baseline, calendar))
    if not ready:
        blockers.extend(sorted(feed_blockers))
        if not feeds:
            blockers.append("full_market_baseline_missing")
    capacity: dict = {}
    return {
        "enabled": profile == "pilot" or settings.FULL_MARKET_ENABLED,
        "execution_profile": profile,
        "environment": settings.APP_ENVIRONMENT,
        "blockers": blockers,
        "feeds": feeds,
        "ready": ready,
        "enrollment": None,
        "capacity": capacity,
    }


async def acquisition_eligibility(
    db: AsyncSession,
    provider: str,
    datasets: list[DatasetRegistry],
    *,
    profile: ExecutionProfile = "full_market",
) -> dict:
    result = await eligibility(db, provider, datasets, profile=profile)
    lifecycle = await db.get(FullMarketEnvironment, get_settings().APP_ENVIRONMENT)
    if profile == "full_market" and (
        not result["enabled"] or lifecycle is None or not lifecycle.enabled
    ):
        result["blockers"].append("full_market_flag_disabled")
    return result


async def lock_calendars(db: AsyncSession, datasets: list[DatasetRegistry]) -> None:
    await db.execute(
        select(CalendarMarket)
        .where(CalendarMarket.market.in_(sorted({market(item) for item in datasets})))
        .order_by(CalendarMarket.market)
        .with_for_update()
    )


async def freeze_admission(
    db: AsyncSession, row: SchedulerControl, result: dict
) -> FullMarketAdmission:
    profile = scheduler_profile(row.scheduler_key)
    admission = FullMarketAdmission(
        admission_id=uuid7(),
        scheduler_key=row.scheduler_key,
        control_revision=row.revision + 1,
        enrollment_id=None,
        capacity={},
        created_at=utc_now(),
    )
    db.add(admission)
    await db.flush()
    for dataset, baseline, calendar in result["ready"]:
        db.add(
            FullMarketAdmissionFeed(
                admission_id=admission.admission_id,
                dataset_key=dataset.dataset_key,
                baseline_id=baseline.release_id,
                calendar_revision_id=calendar.id if calendar else None,
            )
        )
        state = await db.get(
            FullMarketDatasetState,
            {"dataset_key": dataset.dataset_key, "execution_profile": profile},
        )
        if state is None:
            state = FullMarketDatasetState(
                dataset_key=dataset.dataset_key,
                execution_profile=profile,
                first_start_date=local_today(dataset),
            )
            db.add(state)
        if profile == "pilot":
            continue
        config = dict(dataset.config or {})
        legacy_governance = config.get("full_market")
        governance = dict(legacy_governance) if isinstance(legacy_governance, Mapping) else {}
        governance["activation_date"] = state.first_start_date.isoformat()
        config["full_market"] = governance
        dataset.config = config
    return admission


def _valid_source_binding(client: SourceClient | None, provider: str) -> bool:
    if (
        client is None
        or client.revoked_at
        or (client.expires_at and ensure_utc(client.expires_at) <= utc_now())
        or client.source_name != provider
        or not isinstance(client.allowed_datasets, list)
        or not client.allowed_datasets
        or any(not isinstance(key, str) or not key.strip() for key in client.allowed_datasets)
    ):
        return False
    return set(client.allowed_datasets) <= set(PROVIDER_DATASET_SCOPE.get(provider, ()))


async def admission_projection(
    db: AsyncSession,
    provider: str,
    *,
    source: bool = False,
    profile: ExecutionProfile = "full_market",
) -> dict:
    key = scheduler_key(provider, profile)
    control = await db.get(SchedulerControl, key)
    admission = await db.scalar(
        select(FullMarketAdmission)
        .where(FullMarketAdmission.scheduler_key == key)
        .order_by(FullMarketAdmission.control_revision.desc())
        .limit(1)
    )
    scope = []
    first_dates = {}
    if admission:
        scope = list(
            (
                await db.scalars(
                    select(FullMarketAdmissionFeed.dataset_key)
                    .where(FullMarketAdmissionFeed.admission_id == admission.admission_id)
                    .order_by(FullMarketAdmissionFeed.dataset_key)
                )
            ).all()
        )
    states = await db.scalars(
        select(FullMarketDatasetState).where(
            FullMarketDatasetState.execution_profile == profile,
            FullMarketDatasetState.dataset_key.in_(PROVIDER_DATASET_SCOPE.get(provider, ())),
        )
    )
    first_dates = {state.dataset_key: state.first_start_date.isoformat() for state in states}
    datasets = list(
        (
            await db.scalars(select(DatasetRegistry).where(DatasetRegistry.dataset_key.in_(scope)))
        ).all()
    )
    result = await acquisition_eligibility(db, provider, datasets, profile=profile)
    allowed = bool(
        control
        and control.desired_state == "running"
        and admission
        and not result["blockers"]
        and len(result["ready"]) == len(scope)
    )
    if source:
        client_id = db.info.get("source_client_id")
        client = await db.get(SourceClient, client_id) if client_id else None
        if not _valid_source_binding(client, provider) or not set(scope) <= set(
            (client.allowed_datasets or []) if client else []
        ):
            allowed = False
            result["blockers"].append("full_market_source_binding_invalid")
    from app.services.daily_runtime import daily_window

    runtime_window = daily_window(provider)
    if not runtime_window["open"]:
        allowed = False
        result["blockers"].append("daily_acquisition_window_closed")
    return {
        "daily_runtime_window": runtime_window,
        "full_market_enabled": get_settings().FULL_MARKET_ENABLED,
        "execution_profile": profile,
        "environment": result["environment"],
        "admission_id": str(admission.admission_id) if admission else None,
        "admitted_dataset_keys": scope,
        "first_start_dates": first_dates,
        "acquisition_allowed": allowed,
        "acquisition_blockers": result["blockers"],
        "declaration_sha256": None,
    }


async def account_authorization(
    db: AsyncSession,
    provider: str,
    consumer: str,
    digest: str,
    runtime_id: str,
    *,
    profile: ExecutionProfile = "full_market",
) -> dict:
    if consumer in {"pilot", "full_market"}:
        profile = "pilot" if consumer == "pilot" else "full_market"
        return await admission_projection(db, provider, source=True, profile=profile)
    if consumer != "maintenance":
        return {"acquisition_allowed": False, "declaration_sha256": None}
    client_id = db.info.get("source_client_id")
    client = await db.get(SourceClient, client_id) if client_id else None
    lifecycle = await db.get(FullMarketEnvironment, get_settings().APP_ENVIRONMENT)
    blockers = []
    if profile == "full_market" and (
        not get_settings().FULL_MARKET_ENABLED or lifecycle is None or not lifecycle.enabled
    ):
        blockers.append("full_market_flag_disabled")
    if not _valid_source_binding(client, provider):
        blockers.append("full_market_source_binding_invalid")
    from app.services.hk_provider_cutover import acquisition_allowed

    authorized = []
    if client and not blockers:
        for dataset in await db.scalars(
            select(DatasetRegistry).where(
                DatasetRegistry.dataset_key.in_(client.allowed_datasets or [])
            )
        ):
            if acquisition_allowed(dataset, provider, maintenance=True):
                authorized.append(dataset.dataset_key)
    return {
        "acquisition_allowed": not blockers and bool(authorized),
        "acquisition_blockers": blockers,
        "authorized_dataset_keys": sorted(authorized),
        "declaration_sha256": None,
    }
