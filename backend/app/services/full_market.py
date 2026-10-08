"""Governed, frozen full-market membership and daily delivery reconciliation."""

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import (
    CalendarRevisionDay,
    CalendarYearRevision,
    FuturesContract,
    FuturesContractEOD,
    Instrument,
    MarketDataEOD,
    MarketDataMinute,
)
from app.models.registry import (
    DailyDeliveryMember,
    DailyDeliveryPart,
    DailyDeliveryPlan,
    DatasetRegistry,
    IngestionRun,
    UniverseMember,
    UniverseRelease,
)
from app.schemas.full_market import (
    DeliveryOutcomeRequest,
    DeliveryPartResponse,
    DeliveryPlanCreateRequest,
    DeliveryPlanResponse,
    DeliverySummaryResponse,
    UniverseListResponse,
    UniverseMemberInput,
    UniversePublishRequest,
    UniverseReleaseDetailResponse,
    UniverseReleaseResponse,
    UniverseSubmitRequest,
)
from app.schemas.ingress import (
    FuturesEODIngressRequest,
    FuturesEODRow,
    IngressRequestV1,
    MarketEODRow,
    MarketMinuteRow,
)
from app.services.calendar_management import complete_published_revision_ids
from app.services.ingress_contracts import parse_dataset_contract_declaration
from app.services.sequenced_snapshots import list_sequenced_snapshot_groups
from app.utils import utc_now, uuid7


class FullMarketError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _member_key(member: UniverseMemberInput, session: str | None = None) -> str:
    if member.asset_class == "future":
        assert member.product_code and member.contract_code and session
        return f"{member.product_code}|{member.contract_code}|{session}"
    return member.symbol


def _expanded_members(body: UniverseSubmitRequest) -> list[dict]:
    members: list[dict] = []
    for member in body.members:
        sessions: list[str | None] = list(member.sessions) if member.sessions else [None]
        for session in sessions:
            values = member.model_dump(exclude={"sessions"})
            values["session"] = session
            values["member_key"] = _member_key(member, session)
            members.append(values)
    members.sort(key=lambda value: value["member_key"])
    if len(members) > 30000 or len({member["member_key"] for member in members}) != len(members):
        raise FullMarketError("universe members are duplicated or exceed the limit", 422)
    return members


async def _scope(
    db: AsyncSession,
    *,
    dataset_key: str,
    provider: str,
    allowed_datasets: list[str] | None,
    lock: bool = False,
) -> DatasetRegistry:
    if not allowed_datasets or dataset_key not in allowed_datasets:
        raise FullMarketError("Source dataset scope denied", 403)
    stmt = select(DatasetRegistry).where(DatasetRegistry.dataset_key == dataset_key)
    if lock:
        stmt = stmt.with_for_update()
    dataset = (await db.execute(stmt)).scalar_one_or_none()
    if dataset is None:
        raise FullMarketError("Dataset not found", 404)
    declaration = parse_dataset_contract_declaration(dataset.config)
    if declaration is None or provider not in declaration.provider_scope():
        raise FullMarketError("Provider scope denied", 403)
    if not isinstance((dataset.config or {}).get("full_market"), dict):
        raise FullMarketError("Full-market governance is not configured", 409)
    return dataset


def _release_response(release: UniverseRelease) -> UniverseReleaseResponse:
    return UniverseReleaseResponse(
        release_id=release.release_id,
        dataset_key=release.dataset_key,
        provider=release.provider,
        effective_date=release.effective_date,
        status=cast(Literal["candidate", "published"], release.status),
        member_count=release.member_count,
        member_sha256=release.member_sha256,
        change_count=release.change_count,
        change_ratio=float(release.change_ratio),
    )


async def submit_universe(
    db: AsyncSession,
    body: UniverseSubmitRequest,
    *,
    allowed_datasets: list[str] | None,
) -> UniverseReleaseResponse:
    dataset = await _scope(
        db,
        dataset_key=body.dataset_key,
        provider=body.provider,
        allowed_datasets=allowed_datasets,
        lock=True,
    )
    members = _expanded_members(body)
    if any(member["asset_class"] != dataset.asset_class for member in members):
        raise FullMarketError("Universe asset class differs from dataset", 422)
    if dataset.market == "HK" and any(
        member["exchange"].upper() not in {"HKEX", "SEHK", "XHKG"} for member in members
    ):
        raise FullMarketError("HK members require HKEX/SEHK/XHKG exchange", 422)
    if dataset.dataset_key == "tw_futures_eod" and any(
        member["exchange"].upper() not in {"TAIFEX", "XTAF"} for member in members
    ):
        raise FullMarketError("Futures members require TAIFEX/XTAF exchange", 422)
    digest = _digest(members)
    existing = (
        await db.execute(
            select(UniverseRelease).where(
                UniverseRelease.dataset_key == body.dataset_key,
                UniverseRelease.provider == body.provider,
                UniverseRelease.effective_date == body.effective_date,
                UniverseRelease.member_sha256 == digest,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _release_response(existing)
    prior = (
        await db.execute(
            select(UniverseRelease)
            .where(
                UniverseRelease.dataset_key == body.dataset_key,
                UniverseRelease.provider == body.provider,
                UniverseRelease.status == "published",
            )
            .order_by(UniverseRelease.effective_date.desc(), UniverseRelease.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    previous_members: dict[str, dict] = {}
    if prior is not None:
        prior_members = (
            await db.scalars(
                select(UniverseMember).where(UniverseMember.release_id == prior.release_id)
            )
        ).all()
        previous_members = {
            member.member_key: {
                column.name: getattr(member, column.name)
                for column in UniverseMember.__table__.columns
                # Daily evidence references change even when the member does not.
                # All identity, mapping and classification fields remain material.
                if column.name not in {"member_id", "release_id", "raw_evidence_ref"}
            }
            for member in prior_members
        }
    current_members = {
        member["member_key"]: {
            key: value for key, value in member.items() if key != "raw_evidence_ref"
        }
        for member in members
    }
    change_count = sum(
        previous_members.get(key) != current_members.get(key)
        for key in previous_members.keys() | current_members.keys()
    )
    ratio = Decimal(change_count) / Decimal(
        len(previous_members) if previous_members else len(current_members)
    )
    status = (
        "candidate"
        if prior is None or change_count > 20 or ratio > Decimal("0.02")
        else "published"
    )
    now = utc_now()
    release = UniverseRelease(
        release_id=uuid7(),
        dataset_key=body.dataset_key,
        provider=body.provider,
        effective_date=body.effective_date,
        observed_at=body.observed_at,
        source_timezone=body.source_timezone,
        evidence=[reference.model_dump(mode="json") for reference in body.evidence],
        member_sha256=digest,
        member_count=len(members),
        change_count=change_count,
        change_ratio=ratio,
        status=status,
        published_at=now if status == "published" else None,
        published_by="policy" if status == "published" else None,
        created_at=now,
    )
    db.add(release)
    await db.flush()
    for member in members:
        db.add(UniverseMember(member_id=uuid7(), release_id=release.release_id, **member))
    await db.commit()
    return _release_response(release)


async def list_universes(
    db: AsyncSession,
    *,
    dataset_key: str,
    provider: str,
    allowed_datasets: list[str] | None,
    as_of: date | None = None,
) -> UniverseListResponse:
    await _scope(db, dataset_key=dataset_key, provider=provider, allowed_datasets=allowed_datasets)
    filters = [UniverseRelease.dataset_key == dataset_key, UniverseRelease.provider == provider]
    if as_of is not None:
        filters.append(UniverseRelease.effective_date <= as_of)
    order = (UniverseRelease.effective_date.desc(), UniverseRelease.created_at.desc())
    releases = (
        await db.scalars(select(UniverseRelease).where(*filters).order_by(*order).limit(50))
    ).all()
    published = await db.scalar(
        select(UniverseRelease.release_id)
        .where(*filters, UniverseRelease.status == "published")
        .order_by(*order)
        .limit(1)
    )
    from app.services.full_market_admission import admission_projection

    projection = await admission_projection(db, provider, source=True)
    return UniverseListResponse(
        data=[_release_response(release) for release in releases],
        published_release_id=published,
        activation={
            "enabled": projection["acquisition_allowed"]
            and dataset_key in projection["admitted_dataset_keys"],
            "activation_date": projection["first_start_dates"].get(dataset_key),
            **projection,
        },
    )


async def get_universe(
    db: AsyncSession,
    release_id: UUID,
    *,
    provider: str,
    allowed_datasets: list[str] | None,
) -> UniverseReleaseDetailResponse:
    release = await db.get(UniverseRelease, release_id)
    if release is None:
        raise FullMarketError("Universe release not found", 404)
    await _scope(
        db, dataset_key=release.dataset_key, provider=provider, allowed_datasets=allowed_datasets
    )
    if release.provider != provider:
        raise FullMarketError("Provider scope denied", 403)
    members = (
        await db.scalars(
            select(UniverseMember)
            .where(UniverseMember.release_id == release_id)
            .order_by(UniverseMember.member_key)
        )
    ).all()
    return UniverseReleaseDetailResponse(
        **_release_response(release).model_dump(),
        members=[
            {
                column.name: getattr(member, column.name)
                for column in UniverseMember.__table__.columns
                if column.name not in {"member_id", "release_id"}
            }
            for member in members
        ],
        evidence=release.evidence,
    )


def _calendar_market(dataset: DatasetRegistry) -> str:
    return "TAIFEX" if dataset.dataset_key == "tw_futures_eod" else dataset.market


def _exchange_timezone(dataset: DatasetRegistry) -> ZoneInfo:
    zones = {"US": "America/New_York", "HK": "Asia/Hong_Kong", "TW": "Asia/Taipei"}
    if dataset.market not in zones:
        raise FullMarketError("Full-market exchange timezone is not configured", 409)
    return ZoneInfo(zones[dataset.market])


async def _require_closed_acceptance_day(
    db: AsyncSession,
    dataset: DatasetRegistry,
    trade_date: date,
    *,
    now: datetime,
    session: Literal["regular", "after_hours"] = "regular",
) -> None:
    local_now = now.astimezone(_exchange_timezone(dataset))
    if trade_date > local_now.date():
        raise FullMarketError("Acceptance requires actual elapsed exchange trading days", 409)
    day = (
        await db.scalars(
            select(CalendarRevisionDay)
            .join(
                CalendarYearRevision,
                CalendarYearRevision.id == CalendarRevisionDay.calendar_revision_id,
            )
            .where(
                CalendarYearRevision.market == _calendar_market(dataset),
                CalendarYearRevision.id.in_(complete_published_revision_ids()),
                CalendarRevisionDay.trade_date == trade_date,
                CalendarRevisionDay.is_open.is_(True),
            )
        )
    ).one_or_none()
    if day is None:
        raise FullMarketError("Acceptance requires a published exchange trading day", 409)
    # A published per-day close preserves official early-closing sessions.
    # Missing hours use conservative regular-session policy, never weekdays.
    conservative_closes = {
        "US": time(16),
        "HK": time(16, 10),
        "TW": time(13, 30),
        "TAIFEX": time(13, 45),
    }
    close_time = (
        time(5)
        if _calendar_market(dataset) == "TAIFEX" and session == "after_hours"
        else day.session_close or conservative_closes[_calendar_market(dataset)]
    )
    closed_at = datetime.combine(trade_date, close_time, _exchange_timezone(dataset))
    if local_now < closed_at:
        raise FullMarketError("Acceptance exchange session has not closed", 409)


async def _require_elapsed_delivery(
    db: AsyncSession,
    dataset: DatasetRegistry,
    trade_date: date,
    *,
    observed_at: datetime,
    sessions: set[Literal["regular", "after_hours"]],
    action: str,
    timestamp_field: str,
) -> None:
    """Final data and no-data claims require elapsed server and source clocks."""
    now = utc_now()
    if trade_date > now.astimezone(_exchange_timezone(dataset)).date():
        raise FullMarketError(f"{action} cannot target a future exchange trading date", 409)
    if observed_at > now + timedelta(minutes=5):
        raise FullMarketError(f"{action} {timestamp_field} exceeds allowed clock skew", 409)
    for session in sessions:
        for clock in (now, observed_at):
            await _require_closed_acceptance_day(
                db, dataset, trade_date, now=clock, session=session
            )


async def _require_exchange_open(
    db: AsyncSession, dataset: DatasetRegistry, trade_date: date
) -> None:
    calendar_market = _calendar_market(dataset)
    day = (
        await db.scalars(
            select(CalendarRevisionDay)
            .join(
                CalendarYearRevision,
                CalendarYearRevision.id == CalendarRevisionDay.calendar_revision_id,
            )
            .where(
                CalendarYearRevision.market == calendar_market,
                CalendarYearRevision.id.in_(complete_published_revision_ids()),
                CalendarRevisionDay.trade_date == trade_date,
                CalendarRevisionDay.is_open.is_(True),
            )
        )
    ).first()
    if day is None:
        raise FullMarketError("Complete published exchange calendar has no open trading day", 409)


def _deadline(dataset: DatasetRegistry, trade_date: date) -> datetime:
    # Calendar date is the exchange-attributed date. US completion is due the
    # next Taipei day; HK/TW/TAIFEX are due 23:00 on that Taipei date.
    taipei_date = trade_date + timedelta(days=1 if dataset.market == "US" else 0)
    hour = 9 if dataset.market == "US" else 23
    return datetime.combine(taipei_date, time(hour), ZoneInfo("Asia/Taipei")).astimezone(
        timezone.utc
    )


async def create_plan(
    db: AsyncSession,
    body: DeliveryPlanCreateRequest,
    *,
    allowed_datasets: list[str] | None,
) -> DeliveryPlanResponse:
    from app.services.full_market_admission import lock_calendars, lock_environment

    await lock_environment(db)
    from app.models.registry import FullMarketEnrollment, SchedulerControl, SourceClient
    from app.services.full_market_admission import admission_projection, current_enrollment
    from app.services.scheduler_start import scheduler_start_datasets

    datasets = await scheduler_start_datasets(db, f"full_market_{body.provider}_v1", lock=True)
    dataset = await _scope(
        db,
        dataset_key=body.dataset_key,
        provider=body.provider,
        allowed_datasets=allowed_datasets,
        lock=False,
    )
    await lock_calendars(db, datasets)
    enrollment = await current_enrollment(db, body.provider)
    if enrollment:
        await db.get(
            FullMarketEnrollment,
            enrollment.enrollment_id,
            with_for_update=True,
            populate_existing=True,
        )
        await db.get(
            SourceClient, enrollment.source_client_id, with_for_update=True, populate_existing=True
        )
    await db.get(
        SchedulerControl,
        f"full_market_{body.provider}_v1",
        with_for_update=True,
        populate_existing=True,
    )
    projection = await admission_projection(db, body.provider, source=True)
    activation_value = projection["first_start_dates"].get(body.dataset_key)
    # Existing frozen plans can always be read and completed through their scoped endpoints.
    if (
        not projection["acquisition_allowed"]
        or body.dataset_key not in projection["admitted_dataset_keys"]
        or not activation_value
    ):
        raise FullMarketError("Full-market acquisition is not admitted", 409)
    activation_date = date.fromisoformat(activation_value)
    if body.trade_date < activation_date:
        raise FullMarketError("Pre-first-start delivery plans are forbidden", 409)
    await _require_exchange_open(db, dataset, body.trade_date)
    existing = (
        await db.scalars(
            select(DailyDeliveryPlan).where(
                DailyDeliveryPlan.dataset_key == body.dataset_key,
                DailyDeliveryPlan.provider == body.provider,
                DailyDeliveryPlan.trade_date == body.trade_date,
            )
        )
    ).one_or_none()
    if existing is not None:
        if existing.release_id != body.release_id:
            raise FullMarketError("Daily plan is frozen to another release", 409)
        return await get_plan(
            db, existing.plan_id, provider=body.provider, allowed_datasets=allowed_datasets
        )
    release = await db.get(UniverseRelease, body.release_id)
    if (
        release is None
        or release.dataset_key != body.dataset_key
        or release.provider != body.provider
    ):
        raise FullMarketError("Universe release scope mismatch", 409)
    if release.status != "published" or release.effective_date > body.trade_date:
        raise FullMarketError("Plan requires an applicable published universe", 409)
    latest = (
        await db.scalars(
            select(UniverseRelease)
            .where(
                UniverseRelease.dataset_key == body.dataset_key,
                UniverseRelease.provider == body.provider,
                UniverseRelease.status == "published",
                UniverseRelease.effective_date <= body.trade_date,
            )
            .order_by(UniverseRelease.effective_date.desc(), UniverseRelease.created_at.desc())
            .limit(1)
        )
    ).one_or_none()
    if latest is None or latest.release_id != release.release_id:
        raise FullMarketError("Plan must use latest published release for the date", 409)
    members = list(
        (
            await db.scalars(
                select(UniverseMember)
                .where(UniverseMember.release_id == release.release_id)
                .order_by(UniverseMember.member_key)
            )
        ).all()
    )
    plan = DailyDeliveryPlan(
        plan_id=uuid7(),
        dataset_key=body.dataset_key,
        provider=body.provider,
        trade_date=body.trade_date,
        release_id=release.release_id,
        activation_cutoff=activation_date,
        deadline_at=_deadline(dataset, body.trade_date),
        member_sha256=release.member_sha256,
        expected_count=len(members),
        created_at=utc_now(),
    )
    db.add(plan)
    await db.flush()
    for start in range(0, len(members), 50):
        chunk = members[start : start + 50]
        part_id = uuid7()
        part = DailyDeliveryPart(
            part_id=part_id,
            plan_id=plan.plan_id,
            part_number=start // 50 + 1,
            work_item_id=f"fp1:{part_id.hex}",
            member_sha256=_digest([member.member_key for member in chunk]),
            expected_count=len(chunk),
        )
        db.add(part)
        for member in chunk:
            db.add(
                DailyDeliveryMember(
                    member_id=uuid7(),
                    plan_id=plan.plan_id,
                    part_id=part_id,
                    member_key=member.member_key,
                    symbol=member.symbol,
                    provider_symbol=member.provider_symbol,
                    product_code=member.product_code,
                    contract_code=member.contract_code,
                    session=member.session,
                    mapping_status=member.mapping_status,
                )
            )
    await db.commit()
    return await get_plan(
        db, plan.plan_id, provider=body.provider, allowed_datasets=allowed_datasets
    )


async def _plan_scope(
    db: AsyncSession,
    plan_id: UUID,
    *,
    provider: str,
    allowed_datasets: list[str] | None,
) -> DailyDeliveryPlan:
    plan = await db.get(DailyDeliveryPlan, plan_id)
    if plan is None:
        raise FullMarketError("Delivery plan not found", 404)
    await _scope(
        db, dataset_key=plan.dataset_key, provider=provider, allowed_datasets=allowed_datasets
    )
    if plan.provider != provider:
        raise FullMarketError("Provider scope denied", 403)
    return plan


async def reconcile_plan(
    db: AsyncSession, plan: DailyDeliveryPlan, *, now: datetime | None = None
) -> DeliverySummaryResponse:
    members = list(
        (
            await db.scalars(
                select(DailyDeliveryMember)
                .where(DailyDeliveryMember.plan_id == plan.plan_id)
                .order_by(DailyDeliveryMember.member_key)
            )
        ).all()
    )
    part_ids = {member.part_id for member in members}
    canonical_keys: set[str] = set()
    if plan.dataset_key == "tw_futures_eod":
        futures_rows = (
            await db.execute(
                select(
                    Instrument.symbol,
                    FuturesContract.contract_code,
                    FuturesContractEOD.session,
                    IngestionRun.delivery_part_id,
                )
                .join(FuturesContract, FuturesContract.instrument_id == Instrument.instrument_id)
                .join(
                    FuturesContractEOD,
                    FuturesContractEOD.contract_id == FuturesContract.contract_id,
                )
                .join(IngestionRun, IngestionRun.run_id == FuturesContractEOD.run_id)
                .where(
                    FuturesContractEOD.trade_date == plan.trade_date,
                    IngestionRun.delivery_part_id.in_(part_ids),
                    IngestionRun.status.in_(("completed", "completed_with_errors")),
                )
            )
        ).all()
        canonical_keys = {
            f"{symbol}|{contract}|{session}" for symbol, contract, session, _ in futures_rows
        }
    elif plan.dataset_key in {"tw_equity_minute", "tw_etf_minute"}:
        complete_groups: set[tuple[UUID, str, str]] = set()
        for part_id in part_ids:
            groups = await list_sequenced_snapshot_groups(
                db,
                dataset_key=plan.dataset_key,
                source=plan.provider,
                schema_id="market_minute",
                schema_version=1,
                data_date=plan.trade_date,
                delivery_part_id=part_id,
            )
            complete_groups.update(
                (part_id, group.snapshot_id, group.daily_update_id)
                for group in groups
                if group.complete
            )
        minute_rows = (
            await db.execute(
                select(
                    Instrument.symbol,
                    IngestionRun.delivery_part_id,
                    IngestionRun.snapshot_id,
                    IngestionRun.daily_update_id,
                )
                .join(MarketDataMinute, MarketDataMinute.instrument_id == Instrument.instrument_id)
                .join(IngestionRun, IngestionRun.run_id == MarketDataMinute.run_id)
                .where(
                    MarketDataMinute.trade_date == plan.trade_date,
                    IngestionRun.delivery_part_id.in_(part_ids),
                    IngestionRun.status.in_(("completed", "completed_with_errors")),
                )
            )
        ).all()
        canonical_keys = {
            symbol
            for symbol, part_id, snapshot_id, daily_update_id in minute_rows
            if (part_id, snapshot_id, daily_update_id) in complete_groups
        }
    else:
        eod_rows = (
            await db.execute(
                select(
                    Instrument.symbol,
                    IngestionRun.delivery_part_id,
                )
                .join(MarketDataEOD, MarketDataEOD.instrument_id == Instrument.instrument_id)
                .join(IngestionRun, IngestionRun.run_id == MarketDataEOD.run_id)
                .where(
                    MarketDataEOD.trade_date == plan.trade_date,
                    IngestionRun.delivery_part_id.in_(part_ids),
                    IngestionRun.status.in_(("completed", "completed_with_errors")),
                )
            )
        ).all()
        canonical_keys = {symbol for symbol, _ in eod_rows}
    counts = {"data": 0, "no_data": 0, "missing": 0, "blocked": 0}
    gaps: list[dict] = []
    for member in members:
        if member.member_key in canonical_keys:
            state = "data"
        elif member.outcome == "no_data":
            state = "no_data"
        elif member.outcome == "blocked" or member.mapping_status == "gap":
            state = "blocked"
        else:
            state = "missing"
        counts[state] += 1
        if state in {"missing", "blocked"} and len(gaps) < 1000:
            gaps.append(
                {
                    "member_key": member.member_key,
                    "status": state,
                    "reason": member.outcome_reason
                    or ("mapping_gap" if member.mapping_status == "gap" else None),
                }
            )
    evaluated_at = now or utc_now()
    return DeliverySummaryResponse(
        expected=len(members),
        **counts,
        deadline_at=plan.deadline_at,
        is_late=evaluated_at > plan.deadline_at and counts["missing"] + counts["blocked"] > 0,
        gaps=gaps,
    )


async def get_plan(
    db: AsyncSession,
    plan_id: UUID,
    *,
    provider: str,
    allowed_datasets: list[str] | None,
) -> DeliveryPlanResponse:
    plan = await _plan_scope(db, plan_id, provider=provider, allowed_datasets=allowed_datasets)
    parts = list(
        (
            await db.scalars(
                select(DailyDeliveryPart)
                .where(DailyDeliveryPart.plan_id == plan_id)
                .order_by(DailyDeliveryPart.part_number)
            )
        ).all()
    )
    members = list(
        (
            await db.scalars(
                select(DailyDeliveryMember)
                .where(DailyDeliveryMember.plan_id == plan_id)
                .order_by(DailyDeliveryMember.member_key)
            )
        ).all()
    )
    by_part: dict[UUID, list[str]] = {part.part_id: [] for part in parts}
    for member in members:
        by_part[member.part_id].append(member.member_key)
    summary = await reconcile_plan(db, plan)
    return DeliveryPlanResponse(
        plan_id=plan.plan_id,
        dataset_key=plan.dataset_key,
        provider=plan.provider,
        trade_date=plan.trade_date,
        release_id=plan.release_id,
        deadline_at=plan.deadline_at,
        status="complete" if summary.data + summary.no_data == summary.expected else "incomplete",
        parts=[
            DeliveryPartResponse(
                part_id=part.part_id,
                work_item_id=part.work_item_id,
                member_keys=by_part[part.part_id],
                member_sha256=part.member_sha256,
            )
            for part in parts
        ],
        summary=summary,
    )


async def record_outcome(
    db: AsyncSession,
    plan_id: UUID,
    body: DeliveryOutcomeRequest,
    *,
    provider: str,
    allowed_datasets: list[str] | None,
) -> DeliverySummaryResponse:
    plan = await _plan_scope(db, plan_id, provider=provider, allowed_datasets=allowed_datasets)
    member = (
        await db.scalars(
            select(DailyDeliveryMember)
            .join(DailyDeliveryPart, DailyDeliveryPart.part_id == DailyDeliveryMember.part_id)
            .where(
                DailyDeliveryMember.plan_id == plan_id,
                DailyDeliveryMember.member_key == body.member_key,
                DailyDeliveryPart.work_item_id == body.work_item_id,
            )
            .with_for_update()
        )
    ).one_or_none()
    if member is None:
        raise FullMarketError("Outcome member is outside the frozen plan part", 409)
    if body.outcome == "no_data" and member.mapping_status != "mapped":
        raise FullMarketError("Mapping gaps cannot be classified as no_data", 422)
    if body.outcome == "no_data" and body.evidence is not None:
        if (
            body.evidence.source_symbol != member.provider_symbol
            or body.evidence.trade_date != plan.trade_date
            or body.evidence.session != member.session
        ):
            raise FullMarketError(
                "No-data evidence does not match provider symbol/date/session", 422
            )
        dataset = await db.get(DatasetRegistry, plan.dataset_key)
        assert dataset is not None  # Dataset existence was checked by _plan_scope.
        await _require_elapsed_delivery(
            db,
            dataset,
            plan.trade_date,
            observed_at=body.evidence.observed_at,
            sessions={cast(Literal["regular", "after_hours"], member.session or "regular")},
            action="No-data evidence",
            timestamp_field="observed_at",
        )
    evidence = body.evidence.model_dump(mode="json") if body.evidence else None
    if (
        member.outcome,
        member.outcome_reason,
        member.outcome_evidence,
    ) == (body.outcome, body.reason, evidence):
        return await reconcile_plan(db, plan)
    if member.outcome == "no_data":
        raise FullMarketError("No-data evidence is immutable; resolve through canonical data", 409)
    member.outcome_history = [
        *(member.outcome_history or []),
        {
            "outcome": body.outcome,
            "reason": body.reason,
            "evidence": evidence,
            "recorded_at": utc_now().isoformat(),
        },
    ]
    member.outcome = body.outcome
    member.outcome_reason = body.reason
    member.outcome_evidence = evidence
    member.outcome_at = utc_now()
    await db.commit()
    return await reconcile_plan(db, plan)


async def validate_ingest_part(
    db: AsyncSession, request: IngressRequestV1, dataset: DatasetRegistry
) -> UUID | None:
    """Bind a full-market request to one frozen part before raw persistence."""
    governance = (dataset.config or {}).get("full_market") or {}
    delivery = getattr(request, "delivery", None)
    work_item_id = getattr(delivery, "work_item_id", None)
    if not isinstance(work_item_id, str) or not work_item_id.startswith("fp1:"):
        if governance.get("required"):
            raise FullMarketError("This feed requires a frozen full-market work item", 409)
        return None
    part = (
        await db.scalars(
            select(DailyDeliveryPart).where(DailyDeliveryPart.work_item_id == work_item_id)
        )
    ).one_or_none()
    if part is None:
        raise FullMarketError("Unknown full-market work item", 409)
    plan = await db.get(DailyDeliveryPlan, part.plan_id)
    if plan is None or plan.dataset_key != request.dataset_key or plan.provider != request.source:
        raise FullMarketError("Work item scope mismatch", 409)
    sessions: set[Literal["regular", "after_hours"]] = (
        {row.session for row in request.payload.data}
        if isinstance(request, FuturesEODIngressRequest)
        else {"regular"}
    )
    await _require_elapsed_delivery(
        db,
        dataset,
        plan.trade_date,
        observed_at=request.fetched_at,
        sessions=sessions,
        action="Actual ingress",
        timestamp_field="fetched_at",
    )
    if (
        delivery is None
        or request.payload.batch.data_date != plan.trade_date
        or delivery.target_data_date != plan.trade_date
    ):
        raise FullMarketError("Work item trade date mismatch", 409)
    frozen_members = (
        await db.scalars(
            select(DailyDeliveryMember).where(DailyDeliveryMember.part_id == part.part_id)
        )
    ).all()
    expected = {member.member_key for member in frozen_members}
    universe = {
        member.member_key: member
        for member in (
            await db.scalars(
                select(UniverseMember).where(
                    UniverseMember.release_id == plan.release_id,
                    UniverseMember.member_key.in_(expected),
                )
            )
        ).all()
    }
    if isinstance(request, FuturesEODIngressRequest):
        actual = {
            f"{row.product_code}|{row.contract_code}|{row.session}" for row in request.payload.data
        }
    else:
        actual = {row.symbol for row in request.payload.data}
    if not actual <= expected:
        raise FullMarketError("Payload contains members outside the declared plan part", 409)
    declaration = parse_dataset_contract_declaration(dataset.config)
    assert declaration is not None
    for row in request.payload.data:
        key = (
            f"{row.product_code}|{row.contract_code}|{row.session}"
            if isinstance(row, FuturesEODRow)
            else row.symbol
        )
        frozen = universe[key]
        if frozen.mapping_status != "mapped":
            raise FullMarketError("Mapping-gap members cannot be submitted as data", 409)
        if isinstance(row, (MarketEODRow, MarketMinuteRow)):
            effective_currency = (
                row.currency if isinstance(row, MarketEODRow) else None
            ) or declaration.defaults.currency
            if effective_currency != frozen.currency:
                raise FullMarketError("Payload currency differs from frozen universe", 409)
            if row.source_symbol is not None and row.source_symbol != frozen.provider_symbol:
                raise FullMarketError("Payload provider symbol differs from frozen universe", 409)
    if any(row.trade_date != plan.trade_date for row in request.payload.data):
        raise FullMarketError("Payload row trade date differs from frozen plan", 409)
    return part.part_id


async def publish_universe(
    db: AsyncSession,
    release_id: UUID,
    body: UniversePublishRequest,
    *,
    actor: str,
) -> UniverseReleaseResponse:
    identity = await db.get(UniverseRelease, release_id)
    if identity is None:
        raise FullMarketError("Universe release not found", 404)
    await db.execute(
        select(DatasetRegistry)
        .where(DatasetRegistry.dataset_key == identity.dataset_key)
        .with_for_update()
    )
    release = (
        await db.scalars(
            select(UniverseRelease)
            .where(UniverseRelease.release_id == release_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).one_or_none()
    if release is None:
        raise FullMarketError("Universe release not found", 404)
    if release.status == "published":
        return _release_response(release)
    await db.execute(
        select(DatasetRegistry)
        .where(DatasetRegistry.dataset_key == release.dataset_key)
        .with_for_update()
    )
    prior = (
        await db.scalars(
            select(UniverseRelease)
            .where(
                UniverseRelease.dataset_key == release.dataset_key,
                UniverseRelease.provider == release.provider,
                UniverseRelease.status == "published",
            )
            .order_by(UniverseRelease.effective_date.desc())
            .limit(1)
        )
    ).one_or_none()
    if prior is None and not body.first_baseline_approved:
        raise FullMarketError("First full-market baseline requires explicit audited approval", 409)
    if prior is not None and body.first_baseline_approved:
        raise FullMarketError("First baseline approval is invalid after publication", 409)
    if prior is not None and (release.change_count > 20 or release.change_ratio > Decimal("0.02")):
        if not body.threshold_exception_approved:
            raise FullMarketError(
                "Candidate exceeds change limit and requires explicit exception", 409
            )
    members = list(
        (
            await db.scalars(
                select(UniverseMember)
                .where(UniverseMember.release_id == release_id)
                .order_by(UniverseMember.member_key)
            )
        ).all()
    )
    canonical = [
        {
            column.name: getattr(member, column.name)
            for column in UniverseMember.__table__.columns
            if column.name not in {"member_id", "release_id"}
        }
        for member in members
    ]
    if _digest(canonical) != release.member_sha256:
        raise FullMarketError("Universe membership digest mismatch", 409)
    release.status = "published"
    release.published_at = utc_now()
    release.published_by = actor
    release.approval_note = body.evidence_note
    await db.flush()
    return _release_response(release)


async def activate_feed(db: AsyncSession, dataset_key: str, body: object) -> dict:
    """Retired service entrypoint; callers must use Owner scheduler admission."""
    raise FullMarketError("Feed activation retired; use Owner scheduler PATCH", 410)


async def deactivate_feed(db: AsyncSession, dataset_key: str) -> dict:
    """Retired service entrypoint; callers must use Owner scheduler stop."""
    raise FullMarketError("Feed deactivation retired; use Owner scheduler PATCH", 410)
