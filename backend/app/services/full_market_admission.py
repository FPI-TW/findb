"""Baseline/calendar admission and authenticated acquisition authorization.

Owner start locks environment, sorted datasets, calendar markets, then control.
Acquisition locks the authenticated Source credential before control. Legacy
capability enrollment/report/repair tools remain independent diagnostics.
"""

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.execution_profiles import ExecutionProfile, scheduler_key
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
from app.services.effective_calendar import market_timezone
from app.services.provider_metadata import dataset_calendar_market
from app.services.source_clients import PROVIDER_DATASET_SCOPE
from app.utils import ensure_utc, utc_now, uuid7


class AdmissionError(ValueError):
    pass


def declaration_digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def market(dataset: DatasetRegistry) -> str:
    return dataset_calendar_market(dataset.dataset_key, dataset.market)


def local_today(dataset: DatasetRegistry) -> date:
    try:
        zone = market_timezone(market(dataset))
    except ValueError:
        raise AdmissionError("market_timezone_unsupported") from None
    return utc_now().astimezone(zone).date()


async def lock_environment(db: AsyncSession) -> FullMarketEnvironment:
    environment = get_settings().APP_ENVIRONMENT
    if environment not in {"local", "staging", "production"}:
        raise AdmissionError("environment_invalid")
    await db.execute(
        insert(FullMarketEnvironment)
        .values(environment=environment, updated_at=utc_now())
        .on_conflict_do_nothing()
    )
    return (
        await db.scalars(
            select(FullMarketEnvironment)
            .where(FullMarketEnvironment.environment == environment)
            .with_for_update()
        )
    ).one()


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


_READ_CACHE = "full_market_admission_read_cache"


@dataclass
class _ReadCache:
    """Admission inputs one read request shares across schedulers and feeds."""

    states: dict[tuple[str, str], FullMarketDatasetState | None] = field(default_factory=dict)
    baselines: dict[tuple[str, str, date], UniverseRelease | None] = field(default_factory=dict)
    calendars: dict[tuple[str, int], CalendarYearRevision | None] = field(default_factory=dict)
    admissions: dict[str, tuple[FullMarketAdmission | None, list[str]]] = field(
        default_factory=dict
    )
    datasets: dict[str, DatasetRegistry] = field(default_factory=dict)


@contextmanager
def admission_read_cache(db: AsyncSession) -> Iterator[None]:
    """Share admission inputs for the rest of one read-only request.

    Only Admin reads open it: a write path must see rows it changed itself, so
    it keeps loading fresh inputs on every call.
    """
    owner = _READ_CACHE not in db.info
    if owner:
        db.info[_READ_CACHE] = _ReadCache()
    try:
        yield
    finally:
        if owner:
            db.info.pop(_READ_CACHE, None)


def _cache(db: AsyncSession) -> _ReadCache:
    """The open request cache, or a throwaway one so every call batches its own reads."""
    cache = db.info.get(_READ_CACHE)
    return cache if isinstance(cache, _ReadCache) else _ReadCache()


async def _load_states(
    db: AsyncSession, cache: _ReadCache, keys: Iterable[str], profile: str
) -> None:
    missing = sorted({key for key in keys if (key, profile) not in cache.states})
    if not missing:
        return
    rows = await db.scalars(
        select(FullMarketDatasetState).where(
            FullMarketDatasetState.execution_profile == profile,
            FullMarketDatasetState.dataset_key.in_(missing),
        )
    )
    found = {row.dataset_key: row for row in rows}
    for key in missing:
        cache.states[(key, profile)] = found.get(key)


async def _load_baselines(
    db: AsyncSession, cache: _ReadCache, wanted: Iterable[tuple[str, str, date]]
) -> None:
    """The latest published baseline effective by each dataset's local today, in one read."""
    missing = sorted({item for item in wanted if item not in cache.baselines})
    if not missing:
        return
    rows = await db.scalars(
        select(UniverseRelease)
        .where(
            UniverseRelease.status == "published",
            or_(
                *(
                    and_(
                        UniverseRelease.dataset_key == key,
                        UniverseRelease.provider == provider,
                        UniverseRelease.effective_date <= today,
                    )
                    for key, provider, today in missing
                )
            ),
        )
        .order_by(
            UniverseRelease.dataset_key,
            UniverseRelease.provider,
            UniverseRelease.effective_date.desc(),
            UniverseRelease.created_at.desc(),
        )
        .distinct(UniverseRelease.dataset_key, UniverseRelease.provider)
    )
    found = {(row.dataset_key, row.provider): row for row in rows}
    for key, provider, today in missing:
        cache.baselines[(key, provider, today)] = found.get((key, provider))


async def _load_calendars(
    db: AsyncSession, cache: _ReadCache, wanted: Iterable[tuple[str, int]]
) -> None:
    """Each market-year's complete published revision, scoped per market-year, in one read."""
    missing = sorted({item for item in wanted if item not in cache.calendars})
    if not missing:
        return
    rows = await db.scalars(
        select(CalendarYearRevision).where(
            or_(
                *(
                    and_(
                        CalendarYearRevision.market == calendar_market,
                        CalendarYearRevision.year == year,
                        CalendarYearRevision.id.in_(
                            complete_published_revision_ids(market=calendar_market, year=year)
                        ),
                    )
                    for calendar_market, year in missing
                )
            )
        )
    )
    found: dict[tuple[str, int], CalendarYearRevision] = {}
    for row in rows:
        found.setdefault((row.market, row.year), row)
    for item in missing:
        cache.calendars[item] = found.get(item)


async def _load_eligibility_inputs(
    db: AsyncSession,
    cache: _ReadCache,
    pairs: Iterable[tuple[str, DatasetRegistry]],
    profile: str,
) -> None:
    pairs = list(pairs)
    await _load_states(db, cache, (dataset.dataset_key for _, dataset in pairs), profile)
    todays = [(provider, dataset, local_today(dataset)) for provider, dataset in pairs]
    await _load_baselines(
        db, cache, ((dataset.dataset_key, provider, today) for provider, dataset, today in todays)
    )
    await _load_calendars(
        db, cache, ((market(dataset), today.year) for _, dataset, today in todays)
    )


async def eligibility(
    db: AsyncSession,
    provider: str,
    datasets: list[DatasetRegistry],
    *,
    profile: ExecutionProfile = "full_market",
) -> dict:
    settings = get_settings()
    cache = _cache(db)
    await _load_eligibility_inputs(
        db, cache, ((provider, dataset) for dataset in datasets), profile
    )
    blockers = []
    ready = []
    feeds = []
    feed_blockers: set[str] = set()
    for dataset in datasets:
        reasons: list[str] = []
        today = local_today(dataset)
        state = cache.states.get((dataset.dataset_key, profile))
        baseline = cache.baselines.get((dataset.dataset_key, provider, today))
        if baseline is None:
            reasons.append("full_market_baseline_missing")
        calendar = cache.calendars.get((market(dataset), today.year))
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
    return await eligibility(db, provider, datasets, profile=profile)


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
    profile: ExecutionProfile = "full_market"
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


async def _load_admissions(db: AsyncSession, cache: _ReadCache, keys: Iterable[str]) -> None:
    """Each scheduler's latest admission and its admitted datasets, in two reads."""
    missing = sorted({key for key in keys if key not in cache.admissions})
    if not missing:
        return
    latest = {
        row.scheduler_key: row
        for row in await db.scalars(
            select(FullMarketAdmission)
            .where(FullMarketAdmission.scheduler_key.in_(missing))
            .order_by(
                FullMarketAdmission.scheduler_key, FullMarketAdmission.control_revision.desc()
            )
            .distinct(FullMarketAdmission.scheduler_key)
        )
    }
    scopes: dict[UUID, list[str]] = {row.admission_id: [] for row in latest.values()}
    if scopes:
        for admission_id, dataset_key in (
            await db.execute(
                select(FullMarketAdmissionFeed.admission_id, FullMarketAdmissionFeed.dataset_key)
                .where(FullMarketAdmissionFeed.admission_id.in_(scopes))
                .order_by(FullMarketAdmissionFeed.dataset_key)
            )
        ).all():
            scopes[admission_id].append(dataset_key)
    for key in missing:
        admission = latest.get(key)
        cache.admissions[key] = (admission, scopes[admission.admission_id] if admission else [])


async def _load_datasets(db: AsyncSession, cache: _ReadCache, keys: Iterable[str]) -> None:
    missing = sorted({key for key in keys if key not in cache.datasets})
    if missing:
        for row in await db.scalars(
            select(DatasetRegistry).where(DatasetRegistry.dataset_key.in_(missing))
        ):
            cache.datasets[row.dataset_key] = row


async def prefetch_admission(
    db: AsyncSession, providers: Iterable[str], *, profile: ExecutionProfile = "full_market"
) -> None:
    """Load every input the providers' admission projections need, in a fixed number of reads.

    Within `admission_read_cache` later `admission_projection` and `eligibility`
    calls for these providers then issue no statements of their own.
    """
    cache = _cache(db)
    providers = sorted(set(providers))
    scopes = {provider: PROVIDER_DATASET_SCOPE.get(provider, ()) for provider in providers}
    await _load_admissions(db, cache, (scheduler_key(provider, profile) for provider in providers))
    await _load_datasets(db, cache, (key for keys in scopes.values() for key in keys))
    await _load_eligibility_inputs(
        db,
        cache,
        (
            (provider, cache.datasets[key])
            for provider, keys in scopes.items()
            for key in keys
            if key in cache.datasets
        ),
        profile,
    )


async def admission_projection(
    db: AsyncSession,
    provider: str,
    *,
    source: bool = False,
    profile: ExecutionProfile = "full_market",
) -> dict:
    key = scheduler_key(provider, profile)
    cache = _cache(db)
    control = await db.get(SchedulerControl, key)
    await _load_admissions(db, cache, [key])
    admission, scope = cache.admissions[key]
    scope = list(scope)
    provider_scope = PROVIDER_DATASET_SCOPE.get(provider, ())
    await _load_states(db, cache, provider_scope, profile)
    first_dates = {
        dataset_key: state.first_start_date.isoformat()
        for dataset_key in sorted(provider_scope)
        if (state := cache.states.get((dataset_key, profile))) is not None
    }
    await _load_datasets(db, cache, scope)
    datasets = [
        cache.datasets[dataset_key] for dataset_key in scope if dataset_key in cache.datasets
    ]
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
    if consumer == "full_market":
        return await admission_projection(db, provider, source=True, profile=profile)
    if consumer != "maintenance":
        return {"acquisition_allowed": False, "declaration_sha256": None}
    client_id = db.info.get("source_client_id")
    client = await db.get(SourceClient, client_id) if client_id else None
    blockers = []
    if not _valid_source_binding(client, provider):
        blockers.append("full_market_source_binding_invalid")
    authorized = []
    if client and not blockers:
        authorized = list(
            await db.scalars(
                select(DatasetRegistry.dataset_key).where(
                    DatasetRegistry.dataset_key.in_(client.allowed_datasets or [])
                )
            )
        )
    return {
        "acquisition_allowed": not blockers and bool(authorized),
        "acquisition_blockers": blockers,
        "authorized_dataset_keys": sorted(authorized),
        "declaration_sha256": None,
    }
