"""Trust-bound capability, read-only eligibility, and transactional admission.

Mutation lock order: environment, sorted datasets, sorted CalendarMarket,
enrollment/credential, control, releases/admission. Source reports may only
acknowledge an enrolled declaration; they never create capability.
"""

import hashlib
import json
import math
from datetime import date
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
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
from app.services.full_market_governance import full_market_configuration
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
    calls += {"twelve_data": 4, "finlab": 6, "shioaji": 5, "taifex": 1}[provider]
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
    windows = {"us_equity_eod": 10800, "hk_equity_eod": 14400, "tw_futures_eod": 18000}
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


async def eligibility(db: AsyncSession, provider: str, datasets: list[DatasetRegistry]) -> dict:
    settings = get_settings()
    blockers = []
    lifecycle = await db.get(FullMarketEnvironment, settings.APP_ENVIRONMENT)
    if not settings.FULL_MARKET_ENABLED or lifecycle is None or not lifecycle.enabled:
        blockers.append("full_market_flag_disabled")
    enrollment = await current_enrollment(db, provider)
    proof = None
    if enrollment is None:
        blockers.append("full_market_runtime_missing")
    elif enrollment.reported_at is None:
        blockers.append("full_market_readiness_not_reported")
    elif not enrollment.reported_enabled:
        blockers.append("full_market_runtime_flag_mismatch")
    elif (utc_now() - ensure_utc(enrollment.reported_at)).total_seconds() > 90:
        blockers.append("full_market_installed_runtime_unavailable")
    elif ensure_utc(enrollment.expires_at) <= utc_now():
        blockers.append("full_market_readiness_expired")
    else:
        try:
            proof = ReadinessDeclaration.model_validate(enrollment.declaration)
        except ValueError:
            blockers.append("full_market_readiness_invalid")
        client = await db.get(SourceClient, enrollment.source_client_id)
        if (
            proof is None
            or client is None
            or client.revoked_at
            or (client.expires_at and ensure_utc(client.expires_at) <= utc_now())
            or client.source_name != provider
            or not set(proof.datasets) <= set(client.allowed_datasets or [])
        ):
            blockers.append("full_market_source_binding_invalid")
        elif (
            proof.source_requests_per_minute
            > client.rate_limit_requests * 60 / client.rate_limit_window
        ):
            blockers.append("full_market_source_rate_binding_invalid")
    ready = []
    feeds = []
    counts = {}
    for dataset in datasets:
        _, _, errors, _ = full_market_configuration(dataset, provider)
        reasons = list(errors)
        today = local_today(dataset)
        state = await db.get(FullMarketDatasetState, dataset.dataset_key)
        if proof is None or dataset.dataset_key not in proof.datasets:
            reasons.append("full_market_feed_not_enrolled")
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
        if calendar is None:
            reasons.append("full_market_calendar_missing")
        governance = (dataset.config or {}).get("full_market")
        legacy_present = (
            isinstance(governance, dict) and governance.get("activation_date") is not None
        )
        if legacy_present and state is None:
            reasons.append("full_market_legacy_date_invalid")
        info = {
            "dataset_key": dataset.dataset_key,
            "ready": not reasons,
            "blockers": reasons,
            "first_start_date": state.first_start_date.isoformat() if state else None,
        }
        feeds.append(info)
        if not reasons and baseline and calendar:
            ready.append((dataset, baseline, calendar))
            counts[dataset.dataset_key] = baseline.member_count
    if not ready:
        blockers.append("full_market_no_ready_feeds")
    capacity: dict = {}
    if proof and ready:
        capacity, errors = capacity_proof(proof, provider, counts)
        blockers.extend(errors)
    return {
        "enabled": settings.FULL_MARKET_ENABLED,
        "environment": settings.APP_ENVIRONMENT,
        "blockers": blockers,
        "feeds": feeds,
        "ready": ready,
        "enrollment": enrollment,
        "capacity": capacity,
    }


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
    enrollment = result["enrollment"]
    admission = FullMarketAdmission(
        admission_id=uuid7(),
        scheduler_key=row.scheduler_key,
        control_revision=row.revision + 1,
        enrollment_id=enrollment.enrollment_id,
        capacity=result["capacity"],
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
                calendar_revision_id=calendar.id,
            )
        )
        state = await db.get(FullMarketDatasetState, dataset.dataset_key)
        if state is None:
            state = FullMarketDatasetState(
                dataset_key=dataset.dataset_key, first_start_date=local_today(dataset)
            )
            db.add(state)
        config = dict(dataset.config or {})
        governance = dict(config.get("full_market") or {})
        governance["activation_date"] = state.first_start_date.isoformat()
        config["full_market"] = governance
        dataset.config = config
    return admission


async def admission_projection(db: AsyncSession, provider: str, *, source: bool = False) -> dict:
    key = f"full_market_{provider}_v1"
    control = await db.get(SchedulerControl, key)
    admission = await db.scalar(
        select(FullMarketAdmission)
        .where(FullMarketAdmission.scheduler_key == key)
        .order_by(FullMarketAdmission.control_revision.desc())
        .limit(1)
    )
    enrolled = await current_enrollment(db, provider)
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
        for dataset_key in scope:
            state = await db.get(FullMarketDatasetState, dataset_key)
            first_dates[dataset_key] = state.first_start_date.isoformat() if state else None
    datasets = list(
        (
            await db.scalars(select(DatasetRegistry).where(DatasetRegistry.dataset_key.in_(scope)))
        ).all()
    )
    result = await eligibility(db, provider, datasets)
    allowed = bool(
        control
        and control.desired_state == "running"
        and admission
        and enrolled
        and admission.enrollment_id == enrolled.enrollment_id
        and not result["blockers"]
        and len(result["ready"]) == len(scope)
    )
    if source and (
        enrolled is None or enrolled.source_client_id != db.info.get("source_client_id")
    ):
        allowed = False
    return {
        "full_market_enabled": result["enabled"],
        "environment": result["environment"],
        "admission_id": str(admission.admission_id) if admission else None,
        "admitted_dataset_keys": scope,
        "first_start_dates": first_dates,
        "acquisition_allowed": allowed,
        "acquisition_blockers": result["blockers"],
        "declaration_sha256": enrolled.declaration_sha256 if enrolled else None,
    }


async def account_authorization(
    db: AsyncSession, provider: str, consumer: str, digest: str, runtime_id: str
) -> dict:
    if consumer == "pilot":
        running = await db.scalar(
            select(SchedulerControl.scheduler_key)
            .where(
                SchedulerControl.provider == provider,
                ~SchedulerControl.scheduler_key.startswith("full_market_", autoescape=True),
                SchedulerControl.desired_state == "running",
            )
            .limit(1)
        )
        return {"acquisition_allowed": running is not None, "declaration_sha256": None}
    row = await current_enrollment(db, provider)
    lifecycle = await db.get(FullMarketEnvironment, get_settings().APP_ENVIRONMENT)
    allowed = bool(
        get_settings().FULL_MARKET_ENABLED
        and lifecycle
        and lifecycle.enabled
        and row
        and row.reported_at
        and row.reported_enabled
        and (utc_now() - ensure_utc(row.reported_at)).total_seconds() <= 90
        and not row.revoked_at
        and ensure_utc(row.expires_at) > utc_now()
        and row.source_client_id == db.info.get("source_client_id")
        and row.runtime_id == runtime_id
        and row.declaration_sha256 == digest
        and consumer in row.declaration["account"]["consumers"]
    )
    interval = None
    if allowed and row:
        client = await db.get(SourceClient, row.source_client_id)
        if (
            client is None
            or client.revoked_at
            or (client.expires_at and ensure_utc(client.expires_at) <= utc_now())
        ):
            allowed = False
        else:
            proof = row.declaration
            actual_rpm = client.rate_limit_requests * 60 / client.rate_limit_window
            allowed = allowed and proof["source_requests_per_minute"] <= actual_rpm
            interval = max(
                1 / proof["requests_per_second"],
                60 / proof["requests_per_minute"],
                60 * 8 / (0.8 * min(actual_rpm, proof["source_requests_per_minute"])),
            )
    if consumer == "full_market":
        projection = await admission_projection(db, provider, source=True)
        allowed = allowed and projection["acquisition_allowed"]
    return {
        "acquisition_allowed": allowed,
        "declaration_sha256": row.declaration_sha256 if row else None,
        "effective_acquisition_interval_seconds": interval,
    }
