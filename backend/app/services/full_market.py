"""Governed, frozen full-market membership and daily delivery reconciliation."""

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution_profiles import ExecutionProfile, execution_profile, scheduler_key
from app.models.canonical import (
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
    DailyExecutionDecision,
    DatasetRegistry,
    DeliveryMemberEvidence,
    IngestionRun,
    UniverseMember,
    UniverseRelease,
)
from app.schemas.full_market import (
    CalendarSkipResponse,
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
    IngressRequest,
    MarketEODIngressRequestV2,
    MarketEODRow,
    MarketMinuteRow,
)
from app.services.effective_calendar import effective_days, execution_policy, execution_snapshot
from app.services.ingress_contracts import parse_dataset_contract_declaration
from app.services.sequenced_snapshots import list_sequenced_snapshot_groups
from app.utils import utc_now, uuid7


class FullMarketError(ValueError):
    def __init__(self, message: str, status_code: int = 409, *, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _semantic_members(members: list[dict]) -> list[dict]:
    """Return membership identity without per-observation evidence references."""
    return [
        {key: value for key, value in member.items() if key != "raw_evidence_ref"}
        for member in sorted(members, key=lambda value: value["member_key"])
    ]


def _semantic_member_digest(members: list[dict]) -> str:
    return _digest(_semantic_members(members))


def _contract_identity(member: dict) -> tuple[str, str, str] | None:
    product = member.get("product_code")
    month = member.get("contract_month")
    session = member.get("session")
    if (
        product not in {"TX", "MTX", "TMF", "TE", "TF"}
        or not isinstance(month, str)
        or not re.fullmatch(r"[0-9]{6}([WF][1-5])?", month)
        or session not in {"regular", "after_hours"}
    ):
        return None
    if any(
        (
            member.get("symbol") != product,
            member.get("provider_symbol") != f"{product}:{month}",
            member.get("contract_code") != f"{product}:{month}",
            member.get("exchange", "").upper() not in {"TAIFEX", "XTAF"},
            member.get("currency") != "TWD",
            member.get("asset_class") != "future",
            member.get("mapping_status") != "mapped",
            member.get("classification")
            != ("weekly" if re.search(r"[WF][1-5]$", month) else "monthly"),
        )
    ):
        return None
    return product, month, session


def _taifex_dated_roll_is_safe(
    previous_members: dict[str, dict],
    current_members: dict[str, dict],
    *,
    effective_date: date,
) -> bool:
    """Allow bounded, forward-only official contract lifecycle changes.

    The generic 2% universe threshold is appropriate for equity catalogues but
    ordinary futures expiry replaces several session members at once.  This
    policy remains fail-closed for missing products, identity drift, backwards
    months, or more than one monthly/weekly contract roll per product.
    """
    if not previous_members or not current_members:
        return False
    previous: dict[str, dict[str, set[str]]] = {}
    current: dict[str, dict[str, set[str]]] = {}
    for target, members in ((previous, previous_members), (current, current_members)):
        for member in members.values():
            identity = _contract_identity(member)
            if identity is None:
                return False
            product, month, session = identity
            target.setdefault(product, {}).setdefault(month, set()).add(session)
    if set(previous) != set(current):
        return False
    effective_month = effective_date.strftime("%Y%m")
    for product in sorted(current):
        if any(month[:6] < effective_month for month in current[product]):
            return False
        previous_monthly = {month for month in previous[product] if len(month) == 6}
        current_monthly = {month for month in current[product] if len(month) == 6}
        previous_weekly = set(previous[product]) - previous_monthly
        current_weekly = set(current[product]) - current_monthly
        if any(
            not previous[product][month] <= current[product][month]
            for month in set(previous[product]) & set(current[product])
        ):
            return False
        added_monthly = current_monthly - previous_monthly
        removed_monthly = previous_monthly - current_monthly
        added_weekly = current_weekly - previous_weekly
        removed_weekly = previous_weekly - current_weekly
        if (
            len(added_monthly) > 1
            or len(removed_monthly) > 1
            or len(added_weekly) > 1
            or len(removed_weekly) > 1
        ):
            return False
        if added_monthly and removed_monthly and min(added_monthly) <= max(removed_monthly):
            return False
        if added_weekly and removed_weekly and min(added_weekly) <= max(removed_weekly):
            return False
    return True


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
    if (
        not isinstance(allowed_datasets, list)
        or not allowed_datasets
        or any(not isinstance(key, str) or not key.strip() for key in allowed_datasets)
        or dataset_key not in allowed_datasets
    ):
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
    semantic_digest = _semantic_member_digest(members)
    existing = (
        (
            await db.execute(
                select(UniverseRelease)
                .where(
                    UniverseRelease.dataset_key == body.dataset_key,
                    UniverseRelease.provider == body.provider,
                    UniverseRelease.effective_date == body.effective_date,
                    UniverseRelease.semantic_member_sha256 == semantic_digest,
                )
                .order_by(UniverseRelease.status.desc(), UniverseRelease.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
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
    taifex_roll = bool(
        prior is not None
        and dataset.dataset_key == "tw_futures_eod"
        and body.provider == "taifex"
        and _taifex_dated_roll_is_safe(
            previous_members,
            current_members,
            effective_date=body.effective_date,
        )
    )
    status = (
        "published"
        if prior is not None and (taifex_roll or (change_count <= 20 and ratio <= Decimal("0.02")))
        else "candidate"
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
        semantic_member_sha256=semantic_digest,
        member_count=len(members),
        change_count=change_count,
        change_ratio=ratio,
        status=status,
        published_at=now if status == "published" else None,
        published_by=(
            "policy:taifex_dated_roll"
            if taifex_roll
            else "policy"
            if status == "published"
            else None
        ),
        approval_note=(
            "Validated bounded forward-only TAIFEX dated contract rollover."
            if taifex_roll
            else None
        ),
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
    profile: ExecutionProfile = "full_market",
) -> UniverseListResponse:
    dataset = await _scope(
        db, dataset_key=dataset_key, provider=provider, allowed_datasets=allowed_datasets
    )
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

    projection = await admission_projection(db, provider, source=True, profile=profile)
    return UniverseListResponse(
        data=[_release_response(release) for release in releases],
        published_release_id=published,
        activation={
            "enabled": projection["acquisition_allowed"]
            and dataset_key in projection["admitted_dataset_keys"],
            "activation_date": projection["first_start_dates"].get(dataset_key),
            "acquisition_start_date": (dataset.config or {})
            .get("hk_provider_cutover", {})
            .get("effective_date")
            if provider == "factset"
            else None,
            **projection,
            "execution_policy": execution_policy(provider),
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
    snapshot: dict | None = None,
) -> None:
    zone = ZoneInfo(snapshot["timezone"]) if snapshot else _exchange_timezone(dataset)
    local_now = now.astimezone(zone)
    if trade_date > local_now.date():
        raise FullMarketError("Acceptance requires actual elapsed exchange trading days", 409)
    calendar = (
        snapshot or (await effective_days(db, _calendar_market(dataset), trade_date, trade_date))[0]
    )
    close_time = time.fromisoformat(
        calendar["after_hours_close"]
        if session == "after_hours" and calendar["after_hours_close"]
        else calendar["regular_close"]
    )
    closed_at = datetime.combine(trade_date, close_time, zone)
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
    plan_id: UUID,
) -> None:
    """Final data and no-data claims require elapsed server and source clocks."""
    decision = await db.scalar(
        select(DailyExecutionDecision).where(DailyExecutionDecision.plan_id == plan_id)
    )
    if decision is None:
        raise FullMarketError("Frozen execution decision is missing", 409)
    now = utc_now()
    if trade_date > now.astimezone(_exchange_timezone(dataset)).date():
        raise FullMarketError(f"{action} cannot target a future exchange trading date", 409)
    if observed_at > now + timedelta(minutes=5):
        raise FullMarketError(f"{action} {timestamp_field} exceeds allowed clock skew", 409)
    for session in sessions:
        for clock in (now, observed_at):
            await _require_closed_acceptance_day(
                db, dataset, trade_date, now=clock, session=session, snapshot=decision.snapshot
            )


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
) -> DeliveryPlanResponse | CalendarSkipResponse:
    from app.services.full_market_admission import lock_calendars, lock_environment

    await lock_environment(db)
    from app.models.registry import SchedulerControl, SourceClient
    from app.services.full_market_admission import admission_projection
    from app.services.scheduler_start import scheduler_start_datasets

    datasets = await scheduler_start_datasets(
        db, scheduler_key(body.provider, body.execution_profile), lock=True
    )
    dataset = await _scope(
        db,
        dataset_key=body.dataset_key,
        provider=body.provider,
        allowed_datasets=allowed_datasets,
        lock=False,
    )
    decision = await db.get(
        DailyExecutionDecision,
        {
            "dataset_key": body.dataset_key,
            "provider": body.provider,
            "trade_date": body.trade_date,
            "execution_profile": body.execution_profile,
        },
    )
    if decision is not None:
        if decision.kind == "skipped_calendar":
            return _skip_response(decision)
        plan = await db.get(DailyDeliveryPlan, decision.plan_id)
        if plan is None or plan.release_id != body.release_id:
            raise FullMarketError("Daily plan is frozen to another release", 409)
        return await get_plan(
            db, plan.plan_id, provider=body.provider, allowed_datasets=allowed_datasets
        )
    await lock_calendars(db, datasets)
    if db.info.get("source_client_id"):
        await db.get(
            SourceClient, db.info["source_client_id"], with_for_update=True, populate_existing=True
        )
    await db.get(
        SchedulerControl,
        scheduler_key(body.provider, body.execution_profile),
        with_for_update=True,
        populate_existing=True,
    )
    projection = await admission_projection(
        db, body.provider, source=True, profile=body.execution_profile
    )
    activation_value = projection["first_start_dates"].get(body.dataset_key)
    # Existing frozen plans can always be read and completed through their scoped endpoints.
    if (
        not projection["acquisition_allowed"]
        or body.dataset_key not in projection["admitted_dataset_keys"]
        or not activation_value
    ):
        raise FullMarketError("Full-market acquisition is not admitted", 409)
    from app.services.hk_provider_cutover import acquisition_allowed

    if not acquisition_allowed(dataset, body.provider, trade_date=body.trade_date):
        raise FullMarketError("Provider acquisition is outside its cutover dates", 409)
    activation_date = date.fromisoformat(activation_value)
    if body.trade_date < activation_date:
        raise FullMarketError("Pre-first-start delivery plans are forbidden", 409)
    existing = (
        await db.scalars(
            select(DailyDeliveryPlan).where(
                DailyDeliveryPlan.dataset_key == body.dataset_key,
                DailyDeliveryPlan.provider == body.provider,
                DailyDeliveryPlan.trade_date == body.trade_date,
                DailyDeliveryPlan.execution_profile == body.execution_profile,
            )
        )
    ).one_or_none()
    if existing is not None:
        if existing.release_id != body.release_id:
            raise FullMarketError("Daily plan is frozen to another release", 409)
        return await get_plan(
            db, existing.plan_id, provider=body.provider, allowed_datasets=allowed_datasets
        )
    snapshot = await execution_snapshot(
        db, _calendar_market(dataset), body.trade_date, body.provider
    )
    await _require_closed_acceptance_day(
        db, dataset, body.trade_date, now=utc_now(), snapshot=snapshot
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
    decision = DailyExecutionDecision(
        dataset_key=body.dataset_key,
        provider=body.provider,
        trade_date=body.trade_date,
        execution_profile=body.execution_profile,
        snapshot=snapshot,
        created_at=utc_now(),
        kind="skipped_calendar"
        if snapshot["policy"]["skip_non_trading_days"] and not snapshot["is_open"]
        else "planned",
    )
    if decision.kind == "skipped_calendar":
        db.add(decision)
        await db.commit()
        return _skip_response(decision)
    if body.provider == "taifex" and release.effective_date != body.trade_date:
        raise FullMarketError(
            "TAIFEX new plan requires a universe for the exact trade date",
            code="taifex_dated_universe_required",
        )
    members = list(
        (
            await db.scalars(
                select(UniverseMember)
                .where(UniverseMember.release_id == release.release_id)
                .order_by(UniverseMember.member_key)
            )
        ).all()
    )
    if body.execution_profile == "pilot":

        def identity(member: UniverseMember) -> tuple[str, str]:
            return (member.product_code or member.symbol, member.contract_code or "")

        selected = set(sorted({identity(member) for member in members})[:2])
        members = [member for member in members if identity(member) in selected]
    if not members:
        raise FullMarketError("Published universe has no plan members", 409)
    plan = DailyDeliveryPlan(
        plan_id=uuid7(),
        dataset_key=body.dataset_key,
        provider=body.provider,
        trade_date=body.trade_date,
        release_id=release.release_id,
        execution_profile=body.execution_profile,
        activation_cutoff=activation_date,
        deadline_at=_deadline(dataset, body.trade_date),
        member_sha256=release.member_sha256
        if body.execution_profile == "full_market"
        else _digest([member.member_key for member in members]),
        expected_count=len(members),
        created_at=utc_now(),
    )
    db.add(plan)
    await db.flush()
    decision.plan_id = plan.plan_id
    db.add(decision)
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
    from app.services.delivery_plan_summary import refresh_plan_summary

    await refresh_plan_summary(db, plan.plan_id)
    await db.commit()
    return await get_plan(
        db, plan.plan_id, provider=body.provider, allowed_datasets=allowed_datasets
    )


def _skip_response(row: DailyExecutionDecision) -> CalendarSkipResponse:
    return CalendarSkipResponse(
        execution_profile=execution_profile(row.execution_profile),
        dataset_key=row.dataset_key,
        provider=row.provider,
        trade_date=row.trade_date,
        snapshot=row.snapshot,
        created_at=row.created_at,
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
    db: AsyncSession,
    plan: DailyDeliveryPlan,
    *,
    now: datetime | None = None,
    gap_limit: int | None = 1000,
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
    complete_groups: set[tuple[UUID, str, str]] = set()
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
        complete_groups = set()
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
                .distinct()
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
    evidence = await db.execute(
        select(
            DeliveryMemberEvidence.member_key,
            IngestionRun.delivery_part_id,
            IngestionRun.snapshot_id,
            IngestionRun.daily_update_id,
        )
        .join(IngestionRun, IngestionRun.run_id == DeliveryMemberEvidence.run_id)
        .where(
            IngestionRun.delivery_part_id.in_(part_ids),
            IngestionRun.status.in_(("completed", "completed_with_errors")),
        )
    )
    for member_key, part_id, snapshot_id, daily_update_id in evidence:
        if (
            plan.dataset_key not in {"tw_equity_minute", "tw_etf_minute"}
            or (part_id, snapshot_id, daily_update_id) in complete_groups
        ):
            canonical_keys.add(member_key)
    if plan.provider == "factset" and plan.dataset_key == "hk_equity_eod":
        canonical_keys.update(await _factset_historical_repairs(db, plan, members))
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
        if state in {"missing", "blocked"} and (gap_limit is None or len(gaps) < gap_limit):
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
        execution_profile=execution_profile(plan.execution_profile),
        expected=len(members),
        **counts,
        deadline_at=plan.deadline_at,
        is_late=evaluated_at > plan.deadline_at and counts["missing"] + counts["blocked"] > 0,
        gaps=gaps,
    )


async def _factset_historical_repairs(
    db: AsyncSession, plan: DailyDeliveryPlan, members: list[DailyDeliveryMember]
) -> set[str]:
    """Historical actual writes may resolve monitoring, never settle another work."""
    from app.config import get_settings
    from app.models.provider_work import ProviderWork, ProviderWorkReceipt

    if plan.provider != "factset" or plan.dataset_key != "hk_equity_eod":
        return set()
    evidence = (
        await db.execute(
            select(DeliveryMemberEvidence.member_key, ProviderWork.work_id)
            .join(IngestionRun, IngestionRun.run_id == DeliveryMemberEvidence.run_id)
            .join(ProviderWorkReceipt, ProviderWorkReceipt.run_id == IngestionRun.run_id)
            .join(ProviderWork, ProviderWork.work_id == ProviderWorkReceipt.work_id)
            .where(
                IngestionRun.source == "factset",
                IngestionRun.dataset_key == plan.dataset_key,
                IngestionRun.batch_data_date == plan.trade_date,
                IngestionRun.schema_id == "market_eod",
                IngestionRun.schema_version == 2,
                IngestionRun.status.in_(("completed", "completed_with_errors")),
                ProviderWork.kind == "historical",
                ProviderWork.environment == get_settings().APP_ENVIRONMENT,
                ProviderWork.provider == "factset",
                ProviderWork.dataset_key == plan.dataset_key,
                ProviderWork.trade_date == plan.trade_date,
            )
        )
    ).all()
    expected = {
        member.member_key: member.provider_symbol
        for member in members
        if member.mapping_status == "mapped"
    }
    manifests: dict[UUID, dict[str, str]] = {}
    repaired = set()
    for key, work_id in evidence:
        if key not in expected:
            continue
        if work_id not in manifests:
            work = await db.get(ProviderWork, work_id)
            manifest = work.manifest if work else None
            manifests[work_id] = (
                {
                    row["key"]: row["source_symbol"]
                    for row in manifest["members"]
                    if row.get("mapping_status") == "mapped"
                }
                if manifest
                and manifest.get("contract") == {"schema_id": "market_eod", "schema_version": 2}
                else {}
            )
        if manifests[work_id].get(key) == expected[key]:
            repaired.add(key)
    return repaired


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
        execution_profile=execution_profile(plan.execution_profile),
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
            plan_id=plan.plan_id,
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
    from app.services.delivery_plan_summary import refresh_plan_summary

    await refresh_plan_summary(db, plan.plan_id)
    await db.commit()
    return await reconcile_plan(db, plan)


async def validate_ingest_part(
    db: AsyncSession, request: IngressRequest, dataset: DatasetRegistry
) -> UUID | None:
    """Bind a full-market request to one frozen part before raw persistence."""
    governance = (dataset.config or {}).get("full_market") or {}
    delivery = getattr(request, "delivery", None)
    work_item_id = getattr(delivery, "work_item_id", None)
    if (
        isinstance(request, MarketEODIngressRequestV2)
        and request.source == "factset"
        and isinstance(work_item_id, str)
        and work_item_id.startswith("historical:")
    ):
        from app.services.historical_backfill import (
            HistoricalBackfillError,
            validate_factset_historical_delivery,
        )

        try:
            await validate_factset_historical_delivery(db, request)
        except HistoricalBackfillError as exc:
            raise FullMarketError(str(exc), 409) from exc
        return None
    if (
        isinstance(request, FuturesEODIngressRequest)
        and request.source == "taifex"
        and isinstance(work_item_id, str)
        and work_item_id.startswith("historical:")
    ):
        from app.services.historical_backfill import (
            HistoricalBackfillError,
            validate_taifex_historical_delivery,
        )

        try:
            await validate_taifex_historical_delivery(db, request)
        except HistoricalBackfillError as exc:
            raise FullMarketError(str(exc), 409) from exc
        return None
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
        plan_id=plan.plan_id,
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
    # Submission uses Python string ordering; database collation can differ for
    # punctuation, case and futures session names.
    canonical.sort(key=lambda member: member["member_key"])
    if _digest(canonical) != release.member_sha256:
        raise FullMarketError("Universe membership digest mismatch", 409)
    release.status = "published"
    release.published_at = utc_now()
    release.published_by = actor
    release.approval_note = body.evidence_note
    await db.flush()
    return _release_response(release)


async def _release_member_values(db: AsyncSession, release_id: UUID) -> list[dict]:
    members = list(
        (
            await db.scalars(
                select(UniverseMember)
                .where(UniverseMember.release_id == release_id)
                .order_by(UniverseMember.member_key)
            )
        ).all()
    )
    values = [
        {
            column.name: getattr(member, column.name)
            for column in UniverseMember.__table__.columns
            if column.name not in {"member_id", "release_id"}
        }
        for member in members
    ]
    # Submission uses Python string ordering; database collation can differ for
    # punctuation, case and futures session names.
    values.sort(key=lambda member: member["member_key"])
    return values


async def reconcile_taifex_rollover_candidates(
    db: AsyncSession,
    *,
    apply: bool,
    actor: str = "repair:taifex_dated_roll",
) -> list[dict]:
    """Preview or publish one latest safe TAIFEX candidate per date.

    Existing duplicate candidates remain immutable audit records.  This repair
    only promotes the newest structurally safe candidate when no exact-date
    release is already published, and is therefore idempotent.
    """
    dataset = (
        await db.scalars(
            select(DatasetRegistry)
            .where(DatasetRegistry.dataset_key == "tw_futures_eod")
            .with_for_update()
        )
    ).one_or_none()
    if dataset is None:
        return []
    candidates = list(
        (
            await db.scalars(
                select(UniverseRelease)
                .where(
                    UniverseRelease.dataset_key == "tw_futures_eod",
                    UniverseRelease.provider == "taifex",
                    UniverseRelease.status == "candidate",
                )
                .order_by(UniverseRelease.effective_date, UniverseRelease.created_at)
                .with_for_update()
            )
        ).all()
    )
    latest_by_date: dict[date, UniverseRelease] = {}
    candidate_counts: dict[date, int] = {}
    for candidate in candidates:
        latest_by_date[candidate.effective_date] = candidate
        candidate_counts[candidate.effective_date] = (
            candidate_counts.get(candidate.effective_date, 0) + 1
        )

    results: list[dict] = []
    for effective_date in sorted(latest_by_date):
        candidate = latest_by_date[effective_date]
        exact_published = await db.scalar(
            select(UniverseRelease.release_id).where(
                UniverseRelease.dataset_key == "tw_futures_eod",
                UniverseRelease.provider == "taifex",
                UniverseRelease.effective_date == effective_date,
                UniverseRelease.status == "published",
            )
        )
        if exact_published is not None:
            results.append(
                {
                    "effective_date": effective_date.isoformat(),
                    "release_id": str(candidate.release_id),
                    "candidate_count": candidate_counts[effective_date],
                    "action": "already_published",
                }
            )
            continue
        prior = (
            await db.scalars(
                select(UniverseRelease)
                .where(
                    UniverseRelease.dataset_key == "tw_futures_eod",
                    UniverseRelease.provider == "taifex",
                    UniverseRelease.status == "published",
                    UniverseRelease.effective_date < effective_date,
                )
                .order_by(
                    UniverseRelease.effective_date.desc(),
                    UniverseRelease.created_at.desc(),
                )
                .limit(1)
            )
        ).one_or_none()
        candidate_values = await _release_member_values(db, candidate.release_id)
        full_digest_valid = _digest(candidate_values) == candidate.member_sha256
        semantic_digest_valid = (
            _semantic_member_digest(candidate_values) == candidate.semantic_member_sha256
        )
        prior_values = await _release_member_values(db, prior.release_id) if prior else []
        safe = bool(
            prior
            and full_digest_valid
            and semantic_digest_valid
            and _taifex_dated_roll_is_safe(
                {row["member_key"]: row for row in _semantic_members(prior_values)},
                {row["member_key"]: row for row in _semantic_members(candidate_values)},
                effective_date=effective_date,
            )
        )
        action = "would_publish" if safe else "manual_review"
        if safe and apply:
            candidate.status = "published"
            candidate.published_at = utc_now()
            candidate.published_by = actor
            candidate.approval_note = (
                "Reconciled bounded forward-only TAIFEX dated contract rollover."
            )
            await db.flush()
            action = "published"
        results.append(
            {
                "effective_date": effective_date.isoformat(),
                "release_id": str(candidate.release_id),
                "candidate_count": candidate_counts[effective_date],
                "action": action,
                "full_digest_valid": full_digest_valid,
                "semantic_digest_valid": semantic_digest_valid,
            }
        )
    if apply:
        await db.commit()
    else:
        await db.rollback()
    return results


async def activate_feed(db: AsyncSession, dataset_key: str, body: object) -> dict:
    """Retired service entrypoint; callers must use Owner scheduler admission."""
    raise FullMarketError("Feed activation retired; use Owner scheduler PATCH", 410)


async def deactivate_feed(db: AsyncSession, dataset_key: str) -> dict:
    """Retired service entrypoint; callers must use Owner scheduler stop."""
    raise FullMarketError("Feed deactivation retired; use Owner scheduler PATCH", 410)
