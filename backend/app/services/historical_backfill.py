"""Durable, provider-neutral historical backfill control plane.

The backend only validates and leases work.  Fetcher owns credentials and
acquisition, then submits data through the normal raw-first Source ingest API.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.registry import (
    DailyDeliveryPlan,
    DatasetRegistry,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
    IngestionRun,
    SchedulerControl,
    SchedulerDataset,
    UniverseMember,
    UniverseMemberListing,
    UniverseRelease,
    UniverseSupplement,
)
from app.schemas.eod_completion import EODCompletionEvidence
from app.schemas.historical_backfill import HistoricalResultEvidence
from app.schemas.ingress import (
    ExchangeDailyQuoteIngressRequest,
    MarketEODIngressRequest,
    MarketEODIngressRequestV2,
)
from app.services.effective_calendar import effective_days, execution_policy, market_timezone
from app.services.feed_schedule import data_ready_at
from app.services.provider_metadata import (
    calendar_required,
    catalog_feed,
    dataset_calendar_market,
    historical_cohort,
    historical_lookback_days,
    provider_catalog,
)
from app.services.universe_supplement import latest_supplement
from app.utils import utc_now, uuid7

# New-request admission only. Retired providers' requests, receipts and query
# paths remain available independently of this installed-executor inventory.
CAPABILITIES: dict[tuple[str, str], str] = {
    (feed.provider, feed.dataset_key): feed.capabilities.historical
    for feed in provider_catalog().feeds
    if feed.capabilities.historical != "unsupported"
}
STAGING_MAX_BACKFILL_DAYS = 31
PRODUCTION_MAX_BACKFILL_DAYS = 366
# Compatibility for existing staging callers; production uses the target-aware helper.
MAX_BACKFILL_DAYS = STAGING_MAX_BACKFILL_DAYS
# Cutoffs follow each feed's calendar market, matching the Fetcher clock; one
# provider's feeds may sit in different markets.
FEED_TIMEZONES: dict[tuple[str, str], str] = {
    (feed.provider, feed.dataset_key): market_timezone(feed.calendar_market).key
    for feed in provider_catalog().feeds
}
# Retired providers without catalog feeds; existing TW minute requests stay readable.
RETIRED_PROVIDER_TIMEZONES = {"shioaji": market_timezone("TW").key}


def _max_backfill_days(provider: str | None = None, dataset_key: str | None = None) -> int:
    """The latest calendar days a request may reach; a feed may extend production's."""
    if provider == "shioaji":
        return STAGING_MAX_BACKFILL_DAYS
    if os.getenv("APP_ENVIRONMENT", "staging").strip().lower() != "production":
        return STAGING_MAX_BACKFILL_DAYS
    extended = (
        historical_lookback_days(provider, dataset_key)
        if provider is not None and dataset_key is not None
        else None
    )
    return extended or PRODUCTION_MAX_BACKFILL_DAYS


def _supports_backfill(provider: str, dataset_key: str) -> bool:
    return (provider, dataset_key) in CAPABILITIES


class HistoricalBackfillError(ValueError):
    pass


MAX_COHORT_MEMBERS = 10000
_COHORT_FIELDS = (
    "member_key",
    "symbol",
    "provider_symbol",
    "exchange",
    "currency",
    "asset_class",
    "classification",
    "mapping_status",
    "raw_evidence_ref",
)


# Frozen request snapshot labels. ``current_hkex`` is the wire name of a
# ``current_release`` cohort that the Fetcher executor checks.
DATED_RELEASE_SNAPSHOT = "full_release"
CURRENT_RELEASE_SNAPSHOT = "current_hkex"


def member_cohort(request: HistoricalBackfillRequest) -> bool:
    """A request whose dates are each bound to the Full release that applied to them."""
    snapshot = request.universe_snapshot
    return isinstance(snapshot, dict) and snapshot.get("cohort") == DATED_RELEASE_SNAPSHOT


def current_release_cohort(request: HistoricalBackfillRequest) -> bool:
    """A request bound as a whole to the release current at its creation."""
    snapshot = request.universe_snapshot
    return isinstance(snapshot, dict) and snapshot.get("cohort") == CURRENT_RELEASE_SNAPSHOT


def eod_cohort(request: HistoricalBackfillRequest) -> bool:
    """Requests completed by per-member EOD evidence instead of one Source run."""
    return current_release_cohort(request) or member_cohort(request)


def futures_report_feed(provider: str, dataset_key: str) -> bool:
    """Feeds whose historical dates complete by a futures report's row evidence."""
    feed = catalog_feed(provider, dataset_key)
    return feed is not None and feed.contract.schema_id == "futures_eod"


def member_evidence_feed(provider: str, dataset_key: str) -> bool:
    """Feeds whose historical deliveries are validated against a frozen member cohort."""
    return historical_cohort(provider, dataset_key) in {"dated_release", "current_release"}


def _cohort_digest(members: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(members, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _release_cohort(db: AsyncSession, release_id: UUID, trade_date: date) -> list[dict]:
    """Mapped release members already listed on the date, in member order."""
    rows = await db.execute(
        select(UniverseMemberListing.member_id, UniverseMemberListing.listed_date)
        .join(UniverseMember, UniverseMember.member_id == UniverseMemberListing.member_id)
        .where(UniverseMember.release_id == release_id)
    )
    listed: dict[UUID, date] = {member_id: listed_date for member_id, listed_date in rows}
    members = await db.scalars(
        select(UniverseMember)
        .where(
            UniverseMember.release_id == release_id,
            UniverseMember.mapping_status == "mapped",
        )
        .order_by(UniverseMember.member_key)
    )
    return [
        {field: getattr(member, field) for field in _COHORT_FIELDS}
        for member in members
        if listed.get(member.member_id, trade_date) <= trade_date
    ]


async def _later_releases(db: AsyncSession, release: UniverseRelease) -> list[UniverseRelease]:
    """Releases of the same feed published after ``release``, oldest first."""
    return list(
        await db.scalars(
            select(UniverseRelease)
            .where(
                UniverseRelease.dataset_key == release.dataset_key,
                UniverseRelease.provider == release.provider,
                UniverseRelease.status == "published",
                tuple_(UniverseRelease.effective_date, UniverseRelease.created_at)
                > tuple_(literal(release.effective_date), literal(release.created_at)),
            )
            .order_by(UniverseRelease.effective_date, UniverseRelease.created_at)
        )
    )


async def _supplement(
    db: AsyncSession,
    release_ids: list[UUID],
    trade_date: date,
    listed: set[str],
    catalogue_id: UUID | None = None,
) -> list[dict]:
    """Mapped members of later releases not in the date's own cohort, in member order.

    A later list may name a symbol its date's list missed (a late publication); only
    FinLab's lists know listing dates, so these are requested without counting toward
    expected. The newest release states each member's identity.
    """
    found: dict[str, dict] = {}
    if catalogue_id is not None:
        catalogue = await db.get(UniverseSupplement, catalogue_id)
        # Catalogue symbols the list lacked; a later release's member identity wins.
        for row in catalogue.members if catalogue else ():
            if row["symbol"] not in listed:
                found[row["symbol"]] = {
                    **dict.fromkeys(_COHORT_FIELDS),
                    **{key: value for key, value in row.items() if key in _COHORT_FIELDS},
                    "member_key": row["symbol"],
                    "mapping_status": "mapped",
                }
    if release_ids:
        # One query returns only the difference: each mapped member already listed by
        # the date that the cohort lacks, from the newest release naming it.
        later = await db.scalars(
            select(UniverseMember)
            .join(UniverseRelease, UniverseRelease.release_id == UniverseMember.release_id)
            .outerjoin(
                UniverseMemberListing,
                UniverseMemberListing.member_id == UniverseMember.member_id,
            )
            .where(
                UniverseMember.release_id.in_(release_ids),
                UniverseMember.mapping_status == "mapped",
                or_(
                    UniverseMemberListing.listed_date.is_(None),
                    UniverseMemberListing.listed_date <= trade_date,
                ),
                *([UniverseMember.member_key.not_in(sorted(listed))] if listed else []),
            )
            .order_by(
                UniverseMember.member_key,
                UniverseRelease.effective_date.desc(),
                UniverseRelease.created_at.desc(),
            )
            .distinct(UniverseMember.member_key)
        )
        for member in later:
            found[member.member_key] = {field: getattr(member, field) for field in _COHORT_FIELDS}
    return [found[key] for key in sorted(found)]


# Releases and catalogue supplements are immutable, so a date's verified cohort is
# the same for every delivery of that date; each process keeps the latest few.
_COHORT_CACHE: "OrderedDict[tuple, tuple[list[dict], list[dict]]]" = OrderedDict()
_COHORT_CACHE_SIZE = 8


async def _bind_full_releases(
    db: AsyncSession, *, provider: str, dataset_key: str, days: list[date]
) -> dict:
    """Bind each date to its Full plan's release, else the latest one published by then.

    Releases are immutable, so the request keeps only each date's release
    identity and member digest; the members are re-read and verified at claim.
    """
    bindings: dict[str, dict] = {}
    for day in days:
        plan = await db.scalar(
            select(DailyDeliveryPlan).where(
                DailyDeliveryPlan.dataset_key == dataset_key,
                DailyDeliveryPlan.provider == provider,
                DailyDeliveryPlan.trade_date == day,
                DailyDeliveryPlan.execution_profile == "full_market",
            )
        )
        release = (
            await db.get(UniverseRelease, plan.release_id)
            if plan is not None
            else await db.scalar(
                select(UniverseRelease)
                .where(
                    UniverseRelease.dataset_key == dataset_key,
                    UniverseRelease.provider == provider,
                    UniverseRelease.status == "published",
                    UniverseRelease.effective_date <= day,
                )
                .order_by(UniverseRelease.effective_date.desc(), UniverseRelease.created_at.desc())
                .limit(1)
            )
        )
        if release is None:
            raise HistoricalBackfillError(f"no published universe applies to {day.isoformat()}")
        members = await _release_cohort(db, release.release_id, day)
        later = [later.release_id for later in await _later_releases(db, release)]
        # The catalogue supplement the date's plan froze, else the latest observed by then.
        catalogue = (
            await db.get(UniverseSupplement, plan.supplement_id)
            if plan is not None and plan.supplement_id is not None
            else await latest_supplement(db, dataset_key=dataset_key, provider=provider, day=day)
        )
        catalogue_id = catalogue.supplement_id if catalogue else None
        supplement = await _supplement(
            db, later, day, {member["member_key"] for member in members}, catalogue_id
        )
        if not 1 <= len(members) or len(members) + len(supplement) > MAX_COHORT_MEMBERS:
            raise HistoricalBackfillError(
                f"universe for {day.isoformat()} has no mapped members or exceeds its bound"
            )
        bindings[day.isoformat()] = {
            "release_id": str(release.release_id),
            "effective_date": release.effective_date.isoformat(),
            "member_sha256": release.member_sha256,
            "plan_id": str(plan.plan_id) if plan is not None else None,
            "cohort_sha256": _cohort_digest(members),
            "member_count": len(members),
            # Later releases frozen at creation; their extra members are requested too.
            "supplement_release_ids": [str(release_id) for release_id in later],
            "catalogue_supplement_id": str(catalogue_id) if catalogue_id else None,
            "supplement_sha256": _cohort_digest(supplement),
            "supplement_count": len(supplement),
        }
    return {"version": 3, "cohort": DATED_RELEASE_SNAPSHOT, "dates": bindings}


async def frozen_cohort(db: AsyncSession, item: HistoricalBackfillItem) -> dict | None:
    """The item's immutable EOD cohort: the request's current release or its date's release."""
    request = item.request
    snapshot = request.universe_snapshot
    if current_release_cohort(request):
        return snapshot
    if not member_cohort(request):
        return None
    assert isinstance(snapshot, dict)
    binding = snapshot["dates"].get(item.trade_date.isoformat())
    if not isinstance(binding, dict):
        raise HistoricalBackfillError("historical date has no frozen universe")
    catalogue_id = binding.get("catalogue_supplement_id")
    cache_key = (
        item.trade_date,
        binding["release_id"],
        binding["member_sha256"],
        binding["cohort_sha256"],
        tuple(binding.get("supplement_release_ids", [])),
        catalogue_id,
        binding.get("supplement_sha256"),
    )
    cached = _COHORT_CACHE.get(cache_key)
    if cached is not None:
        _COHORT_CACHE.move_to_end(cache_key)
        return _cohort_snapshot(item, binding, *cached)
    release = await db.get(UniverseRelease, UUID(binding["release_id"]))
    members = (
        await _release_cohort(db, release.release_id, item.trade_date)
        if release is not None
        else []
    )
    # Version 2 requests froze no later releases, so they request no supplement.
    supplement = await _supplement(
        db,
        [UUID(value) for value in binding.get("supplement_release_ids", [])],
        item.trade_date,
        {member["member_key"] for member in members},
        UUID(catalogue_id) if catalogue_id else None,
    )
    if (
        release is None
        or release.member_sha256 != binding["member_sha256"]
        or _cohort_digest(members) != binding["cohort_sha256"]
        or "supplement_sha256" in binding
        and _cohort_digest(supplement) != binding["supplement_sha256"]
    ):
        raise HistoricalBackfillError("historical frozen universe differs from its release")
    _COHORT_CACHE[cache_key] = (members, supplement)
    while len(_COHORT_CACHE) > _COHORT_CACHE_SIZE:
        _COHORT_CACHE.popitem(last=False)
    return _cohort_snapshot(item, binding, members, supplement)


def _cohort_snapshot(
    item: HistoricalBackfillItem, binding: dict, members: list[dict], supplement: list[dict]
) -> dict:
    return {
        "version": 2,
        "cohort": DATED_RELEASE_SNAPSHOT,
        "trade_date": item.trade_date.isoformat(),
        "release_id": binding["release_id"],
        "effective_date": binding["effective_date"],
        "member_sha256": binding["member_sha256"],
        "plan_id": binding["plan_id"],
        # Copies: a caller never changes what later deliveries of the date verify.
        "members": [dict(member) for member in members],
        # Requested, never counted toward expected: data is recorded as unlisted.
        "supplementary": [dict(member) for member in supplement],
    }


class HistoricalBackfillConflictError(HistoricalBackfillError):
    pass


class HistoricalBackfillValidationError(HistoricalBackfillError):
    pass


def _feed_timezone(provider: str, dataset_key: str) -> str | None:
    return FEED_TIMEZONES.get((provider, dataset_key)) or RETIRED_PROVIDER_TIMEZONES.get(provider)


def _feed_current_date(provider: str, dataset_key: str, now: datetime) -> date:
    """Return the feed's market date for the same cutoff as its Fetcher."""
    timezone_name = _feed_timezone(provider, dataset_key)
    if timezone_name is None:
        raise HistoricalBackfillError("feed timezone is not configured")
    if now.tzinfo is None:
        raise HistoricalBackfillError("historical claim clock must be timezone-aware")
    return now.astimezone(ZoneInfo(timezone_name)).date()


def _request_today(provider: str, dataset_key: str) -> date:
    """Creation and preview use the same feed-local date as claims and expiry.

    An unknown feed has no timezone; its scope check rejects it afterwards.
    """
    now = utc_now()
    if _feed_timezone(provider, dataset_key) is None:
        return now.date()
    return _feed_current_date(provider, dataset_key, now)


def expiry_cutoff(provider: str, dataset_key: str, now: datetime) -> date:
    """One read-only feed-local boundary for claims and live acquisition.

    It is the oldest date a new request may still include, so an item expires the
    same day it would no longer be accepted.
    """
    return _feed_current_date(provider, dataset_key, now) - timedelta(
        days=_max_backfill_days(provider, dataset_key) - 1
    )


def _latest_expiry_cutoff(provider: str, now: datetime) -> date:
    """The latest cutoff across the provider's feeds: a superset filter for expiry."""
    keys = [dataset for owner, dataset in FEED_TIMEZONES if owner == provider] or [""]
    return max(expiry_cutoff(provider, dataset, now) for dataset in keys)


def _session_closed(item: HistoricalBackfillItem, now: datetime) -> bool:
    """Use the immutable request snapshot for new acquisition, never live calendar data."""
    snapshot = item.calendar_snapshot
    if now.tzinfo is None:
        raise HistoricalBackfillError("historical acquisition clock must be timezone-aware")
    if not isinstance(snapshot, dict) or snapshot.get("trade_date") != item.trade_date.isoformat():
        raise HistoricalBackfillError("historical calendar snapshot is invalid")
    try:
        ready = data_ready_at(
            item.request.provider, item.request.dataset_key, item.trade_date, snapshot
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoricalBackfillError("historical calendar snapshot is invalid") from exc
    return now >= ready


async def create_request(
    db: AsyncSession,
    *,
    provider: str,
    dataset_key: str,
    start_date: date,
    end_date: date,
    request_key: str,
    created_by: str,
    today: date | None = None,
) -> tuple[HistoricalBackfillRequest, bool]:
    provider = provider.lower()
    cohort = historical_cohort(provider, dataset_key)
    if cohort == "current_release":
        from app.services.provider_work import lock_provider

        await lock_provider(db, provider)
    today = today or _request_today(provider, dataset_key)
    max_days = _max_backfill_days(provider, dataset_key)
    if start_date < today - timedelta(days=max_days - 1) or end_date > today:
        raise HistoricalBackfillError(
            f"backfill dates must be within the latest {max_days} calendar days"
        )
    if (end_date - start_date).days >= max_days:
        raise HistoricalBackfillError(f"backfill range exceeds {max_days} days")
    existing = (
        await db.execute(
            select(HistoricalBackfillRequest)
            .options(selectinload(HistoricalBackfillRequest.items))
            .where(HistoricalBackfillRequest.request_key == request_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.provider != provider
            or existing.dataset_key != dataset_key
            or existing.start_date != start_date
            or existing.end_date != end_date
        ):
            raise HistoricalBackfillConflictError("request_key is already bound to another scope")
        return existing, True
    dataset = await db.get(DatasetRegistry, dataset_key)
    if dataset is None or not dataset.is_active:
        raise HistoricalBackfillError("dataset is not active")
    if not _supports_backfill(provider, dataset_key):
        raise HistoricalBackfillError("provider does not support historical backfill for dataset")
    scope = (
        await db.execute(
            select(SchedulerControl.scheduler_key)
            .join(
                SchedulerDataset, SchedulerDataset.scheduler_key == SchedulerControl.scheduler_key
            )
            .where(
                SchedulerControl.provider == provider, SchedulerDataset.dataset_key == dataset_key
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if scope is None:
        raise HistoricalBackfillError("provider is not active for dataset")
    universe_snapshot = None
    if cohort == "current_release":
        # Publication locks the same dataset; a request gets one immutable current
        # cohort, not different universe dates for its historical items.
        await db.refresh(dataset, with_for_update=True)
        release = await db.scalar(
            select(UniverseRelease)
            .where(
                UniverseRelease.dataset_key == dataset_key,
                UniverseRelease.provider == provider,
                UniverseRelease.status == "published",
                UniverseRelease.effective_date <= today,
            )
            .order_by(UniverseRelease.effective_date.desc(), UniverseRelease.created_at.desc())
            .limit(1)
        )
        if release is None:
            raise HistoricalBackfillError(
                f"{provider} requires a published current universe for {dataset_key}"
            )
        members = list(
            await db.scalars(
                select(UniverseMember)
                .where(UniverseMember.release_id == release.release_id)
                .order_by(UniverseMember.member_key)
            )
        )
        if not 1 <= len(members) <= 10000:
            raise HistoricalBackfillError("current cohort is empty or exceeds its bound")
        universe_snapshot = {
            "version": 1,
            "cohort": CURRENT_RELEASE_SNAPSHOT,
            "release_id": str(release.release_id),
            "effective_date": release.effective_date.isoformat(),
            "member_sha256": release.member_sha256,
            "members": [
                {
                    field: getattr(member, field)
                    for field in (
                        "member_key",
                        "symbol",
                        "provider_symbol",
                        "exchange",
                        "currency",
                        "asset_class",
                        "classification",
                        "mapping_status",
                        "raw_evidence_ref",
                    )
                }
                for member in members
            ],
        }
    calendar = await effective_days(
        db, dataset_calendar_market(dataset_key, dataset.market), start_date, end_date
    )
    unpublished = [day["trade_date"] for day in calendar if day["calendar_source"] != "published"]
    if unpublished and calendar_required(provider, dataset_key):
        raise HistoricalBackfillError(
            "published exchange calendar required for "
            f"{unpublished[0]}..{unpublished[-1]} ({len(unpublished)} days)"
        )
    # Historical dates follow the same calendar switch as the provider's daily plans.
    policy = execution_policy(provider)
    if cohort == "dated_release":
        # Every acquired date backfills its whole mapped Full release, never a static list.
        universe_snapshot = await _bind_full_releases(
            db,
            provider=provider,
            dataset_key=dataset_key,
            days=[
                date.fromisoformat(day["trade_date"])
                for day in calendar
                if not (policy["skip_non_trading_days"] and not day["is_open"])
            ],
        )
    now = utc_now()
    row = HistoricalBackfillRequest(
        request_id=uuid7(),
        request_key=request_key,
        provider=provider,
        dataset_key=dataset_key,
        market=dataset.market,
        start_date=start_date,
        end_date=end_date,
        created_by=created_by,
        status="queued",
        universe_snapshot=universe_snapshot,
    )
    row.items = [
        HistoricalBackfillItem(
            item_id=uuid7(),
            trade_date=date.fromisoformat(day["trade_date"]),
            status="skipped_calendar"
            if policy["skip_non_trading_days"] and not day["is_open"]
            else "pending",
            completed_at=now if policy["skip_non_trading_days"] and not day["is_open"] else None,
            calendar_snapshot={**day, "policy": policy, "snapshot_origin": "creation"},
        )
        for day in calendar
    ]
    if all(item.status == "skipped_calendar" for item in row.items):
        row.status, row.completed_at = "completed", now
    db.add(row)
    await db.flush()
    from app.services.provider_work import enqueue

    for item in row.items:
        if item.status != "pending":
            continue
        snapshot = item.calendar_snapshot
        assert snapshot is not None
        await enqueue(
            db,
            provider=provider,
            stable_key=f"historical:{item.item_id}",
            kind="historical",
            dataset_key=dataset_key,
            trade_date=item.trade_date,
            scope={"item_id": str(item.item_id), "request_id": str(row.request_id)},
            not_before=data_ready_at(provider, dataset_key, item.trade_date, snapshot),
        )
    return row, False


async def list_requests(
    db: AsyncSession, *, page: int, page_size: int
) -> tuple[list[HistoricalBackfillRequest], int]:
    from sqlalchemy import func

    total = int(
        (await db.execute(select(func.count()).select_from(HistoricalBackfillRequest))).scalar_one()
    )
    rows = (
        (
            await db.execute(
                select(HistoricalBackfillRequest)
                .options(selectinload(HistoricalBackfillRequest.items))
                .order_by(
                    HistoricalBackfillRequest.created_at.desc(),
                    HistoricalBackfillRequest.request_id.desc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return list(rows), total


ITEM_SUMMARY_FIELDS = (
    "item_id",
    "trade_date",
    "status",
    "unlisted_symbols",
    "started_at",
    "completed_at",
    "failure_code",
    "failure_message",
    "run_id",
)


async def list_request_summaries(
    db: AsyncSession, *, page: int, page_size: int, include_items: bool = True
) -> tuple[list[tuple[HistoricalBackfillRequest, dict[str, int], list[dict]]], int]:
    """A page of requests with per-status item counts, in a fixed number of reads.

    Items carry only their summary fields; calendar snapshots and result evidence
    stay with the per-request item read, so a page never grows with evidence size.
    """
    from sqlalchemy import func

    total = int(
        (await db.execute(select(func.count()).select_from(HistoricalBackfillRequest))).scalar_one()
    )
    requests = list(
        (
            await db.scalars(
                select(HistoricalBackfillRequest)
                .order_by(
                    HistoricalBackfillRequest.created_at.desc(),
                    HistoricalBackfillRequest.request_id.desc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    ids = [row.request_id for row in requests]
    counts: dict[Any, dict[str, int]] = {request_id: {} for request_id in ids}
    items: dict[Any, list[dict]] = {request_id: [] for request_id in ids}
    if ids:
        for request_id, status, count in (
            await db.execute(
                select(
                    HistoricalBackfillItem.request_id,
                    HistoricalBackfillItem.status,
                    func.count(),
                )
                .where(HistoricalBackfillItem.request_id.in_(ids))
                .group_by(HistoricalBackfillItem.request_id, HistoricalBackfillItem.status)
            )
        ).all():
            counts[request_id][status] = count
        if include_items:
            columns = [getattr(HistoricalBackfillItem, name) for name in ITEM_SUMMARY_FIELDS]
            for row in (
                await db.execute(
                    select(HistoricalBackfillItem.request_id, *columns)
                    .where(HistoricalBackfillItem.request_id.in_(ids))
                    .order_by(HistoricalBackfillItem.request_id, HistoricalBackfillItem.trade_date)
                )
            ).mappings():
                items[row["request_id"]].append({name: row[name] for name in ITEM_SUMMARY_FIELDS})
    return [(row, counts[row.request_id], items[row.request_id]) for row in requests], total


async def list_request_items(
    db: AsyncSession,
    request_id: Any,
    *,
    page: int,
    page_size: int,
    include_evidence: bool = False,
) -> tuple[list[dict], int] | None:
    """One request's items by trade date; evidence only when asked for."""
    from sqlalchemy import func

    if await db.get(HistoricalBackfillRequest, request_id) is None:
        return None
    names = (
        (*ITEM_SUMMARY_FIELDS, "calendar_snapshot", "result_evidence")
        if include_evidence
        else (ITEM_SUMMARY_FIELDS)
    )
    total = int(
        await db.scalar(
            select(func.count())
            .select_from(HistoricalBackfillItem)
            .where(HistoricalBackfillItem.request_id == request_id)
        )
        or 0
    )
    rows = (
        await db.execute(
            select(*(getattr(HistoricalBackfillItem, name) for name in names))
            .where(HistoricalBackfillItem.request_id == request_id)
            .order_by(HistoricalBackfillItem.trade_date)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).mappings()
    return [{name: row[name] for name in names} for row in rows], total


async def list_enabled_scopes(db: AsyncSession) -> list[tuple[str, str, str, bool]]:
    """Expose only configured active scopes; no credential information leaks."""
    rows: list[tuple[str, str, str, bool]] = []
    for (provider, dataset_key), capability in CAPABILITIES.items():
        if not _supports_backfill(provider, dataset_key):
            continue
        dataset = await db.get(DatasetRegistry, dataset_key)
        if dataset is None or not dataset.is_active:
            continue
        active = (
            await db.execute(
                select(SchedulerControl.scheduler_key)
                .join(
                    SchedulerDataset,
                    SchedulerDataset.scheduler_key == SchedulerControl.scheduler_key,
                )
                .where(
                    SchedulerControl.provider == provider,
                    SchedulerDataset.dataset_key == dataset_key,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if active:
            rows.append(
                (
                    provider,
                    dataset_key,
                    dataset.market,
                    capability == "executable" or capability == "declared",
                )
            )
    return rows


async def preview_request(
    db: AsyncSession,
    *,
    provider: str,
    dataset_key: str,
    start_date: date,
    end_date: date,
    today: date | None = None,
) -> tuple[str | None, str | None, list[tuple[date, bool, str | None]]]:
    """Return a no-write operator preview; every selected day is explained."""
    provider = provider.lower()
    today = today or _request_today(provider, dataset_key)
    dataset = await db.get(DatasetRegistry, dataset_key)
    market = dataset.market if dataset is not None else None
    max_days = _max_backfill_days(provider, dataset_key)
    scope_reason: str | None = None
    if end_date < start_date:
        scope_reason = "date_order_invalid"
    elif start_date < today - timedelta(days=max_days - 1) or end_date > today:
        scope_reason = f"outside_latest_{max_days}_days"
    elif dataset is None or not dataset.is_active:
        scope_reason = "dataset_inactive"
    elif not _supports_backfill(provider, dataset_key):
        scope_reason = "provider_dataset_unsupported"
    elif (
        await db.execute(
            select(SchedulerControl.scheduler_key)
            .join(
                SchedulerDataset, SchedulerDataset.scheduler_key == SchedulerControl.scheduler_key
            )
            .where(
                SchedulerControl.provider == provider, SchedulerDataset.dataset_key == dataset_key
            )
            .limit(1)
        )
    ).scalar_one_or_none() is None:
        scope_reason = "provider_scope_inactive"
    if end_date < start_date:
        return market, scope_reason, []
    calendar_rows = (
        {
            date.fromisoformat(day["trade_date"]): day
            for day in await effective_days(
                db, dataset_calendar_market(dataset_key, market), start_date, end_date
            )
        }
        if market is not None
        else {}
    )
    # Creation refuses the whole range, so the preview does too.
    if (
        scope_reason is None
        and calendar_required(provider, dataset_key)
        and any(day["calendar_source"] != "published" for day in calendar_rows.values())
    ):
        scope_reason = "calendar_unpublished"
    # The same switch creation applies; an out-of-scope preview marks no closed days.
    skip_non_trading = scope_reason is None and execution_policy(provider)["skip_non_trading_days"]
    days: list[tuple[date, bool, str | None]] = []
    current = start_date
    while current <= end_date:
        if scope_reason is not None:
            days.append((current, False, scope_reason))
        elif skip_non_trading and not calendar_rows[current]["is_open"]:
            days.append((current, False, "market_closed"))
        else:
            days.append((current, True, None))
        current += timedelta(days=1)
    return market, scope_reason, days


async def _cancel_queued_item_work(
    db: AsyncSession, item: HistoricalBackfillItem, *, provider: str, now: datetime
) -> None:
    """Project a terminal domain decision without abandoning retained deliveries.

    Callers hold the provider transaction lock before locking request/item rows.
    """
    from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkReceipt
    from app.services.provider_work import environment, has_prepared

    work = await db.scalar(
        select(ProviderWork).where(
            ProviderWork.environment == environment(),
            ProviderWork.provider == provider,
            ProviderWork.stable_key == f"historical:{item.item_id}",
        )
    )
    if work is None or work.state != "queued":
        return
    retained = await has_prepared(db, work.work_id) or bool(
        await db.scalar(
            select(ProviderWorkReceipt.run_id)
            .where(ProviderWorkReceipt.work_id == work.work_id)
            .limit(1)
        )
    )
    if not retained:
        work.state, work.completed_at = "cancelled", now
    for attempt in await db.scalars(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work.work_id, ProviderWorkAttempt.state == "queued"
        )
    ):
        if retained:
            attempt.drain_only = True
        else:
            attempt.state, attempt.outcome, attempt.settled_at = "settled", "cancelled", now


async def cancel_request(
    db: AsyncSession, request_id: UUID, *, actor: str
) -> HistoricalBackfillRequest:
    from app.services.provider_work import lock_provider

    initial = await db.get(HistoricalBackfillRequest, request_id)
    if initial is not None:
        await lock_provider(db, initial.provider)
    row = (
        await db.execute(
            select(HistoricalBackfillRequest)
            .options(selectinload(HistoricalBackfillRequest.items))
            .where(HistoricalBackfillRequest.request_id == request_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HistoricalBackfillError("backfill request not found")
    if row.status in {"completed", "partial", "failed", "cancelled"}:
        raise HistoricalBackfillConflictError("backfill request is already terminal")
    now = utc_now()
    row.status, row.cancelled_by, row.cancelled_at = "cancelled", actor, now
    for item in row.items:
        await _cancel_queued_item_work(db, item, provider=row.provider, now=now)
        if item.status in {"pending", "running"}:
            item.status, item.completed_at = "cancelled", now
            item.lease_token, item.lease_expires_at = None, None
    await db.flush()
    return row


async def expire_requests(db: AsyncSession, *, provider: str, now: datetime) -> None:
    """Apply the original window under the provider lock, preserving retained drains."""
    # Rows past the latest feed cutoff are a superset; each is checked against its own feed.
    expiry_date = _latest_expiry_cutoff(provider, now)
    expired = (
        (
            await db.execute(
                select(HistoricalBackfillItem)
                .join(HistoricalBackfillRequest)
                .options(
                    selectinload(HistoricalBackfillItem.request).selectinload(
                        HistoricalBackfillRequest.items
                    )
                )
                .where(
                    HistoricalBackfillRequest.provider == provider,
                    HistoricalBackfillRequest.status.in_(("queued", "running")),
                    HistoricalBackfillItem.status.in_(("pending", "running")),
                    HistoricalBackfillItem.trade_date < expiry_date,
                )
                .order_by(HistoricalBackfillRequest.created_at, HistoricalBackfillItem.trade_date)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .unique()
    )
    for item in expired:
        # Rows are selected before the first request is failed. Skip stale
        # sibling objects in that materialized result so one expired earliest
        # date fails and every later date remains cancelled.
        if item.status not in {"pending", "running"} or item.request.status not in {
            "queued",
            "running",
        }:
            continue
        if item.trade_date >= expiry_cutoff(provider, item.request.dataset_key, now):
            continue
        item.status, item.completed_at = "failed", now
        item.failure_code, item.failure_message = "BACKFILL_EXPIRED", "historical date expired"
        item.lease_token, item.lease_expires_at = None, None
        request = item.request
        request.status, request.completed_at = "failed", now
        request.failure_code, request.failure_message = (
            "BACKFILL_EXPIRED",
            "historical date expired",
        )
        for sibling in request.items:
            if sibling.status in {"pending", "running"}:
                sibling.status, sibling.completed_at = "cancelled", now
                sibling.lease_token, sibling.lease_expires_at = None, None
            await _cancel_queued_item_work(db, sibling, provider=provider, now=now)


async def reconcile_cancelled_queue(
    db: AsyncSession, *, provider: str, now: datetime | None = None
) -> None:
    """Defend FIFO eligibility against expiry or a terminal domain transition."""
    await expire_requests(db, provider=provider, now=now or utc_now())
    for item in await db.scalars(
        select(HistoricalBackfillItem)
        .join(HistoricalBackfillRequest)
        .where(
            HistoricalBackfillRequest.provider == provider,
            HistoricalBackfillRequest.status.in_(("cancelled", "failed")),
            HistoricalBackfillItem.status == "cancelled",
        )
    ):
        await _cancel_queued_item_work(db, item, provider=provider, now=utc_now())


async def claim_next_item(
    db: AsyncSession,
    *,
    provider: str,
    allowed_datasets: list[str] | None,
    now: datetime | None = None,
) -> HistoricalBackfillItem | None:
    """Project the fenced FIFO owner's historical date into its item lease.

    Dates initially enqueue in order, but a delayed tail retry does not block
    later runnable dates. This projection never selects an independent lease.
    """
    now = now or utc_now()

    from app.services.provider_work import ProviderWorkError, fence, has_prepared, lock_provider

    await lock_provider(db, provider)
    await expire_requests(db, provider=provider, now=now)

    identity = db.info.get("provider_work")
    if identity is None:
        raise HistoricalBackfillConflictError("provider work protocol is required")
    try:
        work, _, owner = await fence(
            db,
            provider=provider,
            work_id=identity["work_id"],
            generation=identity["generation"],
            token=identity["token"],
            # A recovered closed round may only replay its prepared
            # receipts and prove completion; per-operation authorization
            # still rejects every new acquisition in that state.
            acquisition=False,
        )
    except ProviderWorkError as exc:
        raise HistoricalBackfillConflictError(str(exc)) from exc
    if work.kind != "historical" or work.dataset_key not in (allowed_datasets or []):
        raise HistoricalBackfillConflictError("provider work historical scope differs")
    item_id = UUID(work.scope.get("item_id", work.scope.get("legacy_id")))
    item = await db.scalar(
        select(HistoricalBackfillItem)
        .options(selectinload(HistoricalBackfillItem.request))
        .where(HistoricalBackfillItem.item_id == item_id)
    )
    from app.models.provider_work import ProviderWorkReceipt

    retained = await has_prepared(db, work.work_id) or bool(
        await db.scalar(
            select(ProviderWorkReceipt.run_id)
            .where(ProviderWorkReceipt.work_id == work.work_id)
            .limit(1)
        )
    )
    cancelled_prepared = bool(item and item.request.status in {"cancelled", "failed"} and retained)
    if (
        item is None
        or (not cancelled_prepared and item.request.status not in {"queued", "running"})
        or not _session_closed(item, now)
    ):
        return None
    if cancelled_prepared:
        item.lease_token, item.lease_expires_at = owner.token, owner.expires_at
        await db.flush()
        return item
    item.status, item.started_at = "running", item.started_at or now
    item.lease_token, item.lease_expires_at = owner.token, owner.expires_at
    item.lease_attempt += 1
    item.request.status, item.request.started_at = "running", item.request.started_at or now
    await db.flush()
    return item


async def authorize_item(
    db: AsyncSession,
    item_id: UUID,
    *,
    provider: str,
    allowed_datasets: list[str] | None,
    lease_token: UUID,
) -> tuple[bool, datetime, datetime | None, UUID, str]:
    """Read the current lease without renewing it or changing work state."""
    row = (
        await db.execute(
            select(HistoricalBackfillItem, HistoricalBackfillRequest)
            .join(HistoricalBackfillRequest)
            .where(
                HistoricalBackfillItem.item_id == item_id,
                HistoricalBackfillRequest.provider == provider,
            )
            .execution_options(populate_existing=True)
        )
    ).first()
    checked_at = utc_now()
    if row is None:
        raise HistoricalBackfillError("backfill item not found")
    item, request = row
    from app.services.source_clients import PROVIDER_DATASET_SCOPE

    expected = PROVIDER_DATASET_SCOPE.get(provider, ())
    if (
        not isinstance(allowed_datasets, list)
        or not allowed_datasets
        or any(not isinstance(key, str) or key not in expected for key in allowed_datasets)
        or len(allowed_datasets) != len(set(allowed_datasets))
        or request.dataset_key not in allowed_datasets
    ):
        raise HistoricalBackfillError("backfill item not found")
    projected_expiry = None
    from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
    from app.services.provider_work import environment

    owner = await db.get(ProviderWorkOwner, (environment(), provider), populate_existing=True)
    attempt = (
        await db.get(ProviderWorkAttempt, owner.attempt_id) if owner and owner.attempt_id else None
    )
    work = await db.get(ProviderWork, attempt.work_id) if attempt else None
    allowed = bool(
        work
        and work.stable_key == f"historical:{item_id}"
        and owner
        and owner.token == lease_token
        and owner.expires_at
        and owner.expires_at > checked_at
        and attempt
        and attempt.state == "open"
        and request.status in {"queued", "running"}
        and item.status == "running"
        and item.trade_date >= expiry_cutoff(provider, request.dataset_key, checked_at)
        and _session_closed(item, checked_at)
    )
    if allowed and owner:
        projected_expiry = owner.expires_at
    return (
        allowed,
        checked_at,
        projected_expiry if allowed else None,
        request.request_id,
        request.dataset_key,
    )


async def complete_item(
    db: AsyncSession,
    item_id: UUID,
    *,
    provider: str,
    outcome: Literal["completed", "partial", "failed"],
    lease_token: UUID,
    run_id: UUID | None,
    failure_code: str | None,
    failure_message: str | None,
    result_evidence: dict | None = None,
) -> HistoricalBackfillItem:
    from app.services import provider_work as queue

    await queue.lock_provider(db, provider)
    item = (
        await db.execute(
            select(HistoricalBackfillItem)
            .join(HistoricalBackfillRequest)
            .options(
                selectinload(HistoricalBackfillItem.request).selectinload(
                    HistoricalBackfillRequest.items
                )
            )
            .where(
                HistoricalBackfillItem.item_id == item_id,
                HistoricalBackfillRequest.provider == provider,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise HistoricalBackfillError("backfill item not found")
    completion_report = {
        "outcome": outcome,
        "run_id": str(run_id) if run_id else None,
        "failure_code": failure_code,
        "failure_message": failure_message,
        "result_evidence": result_evidence,
    }
    eod = eod_cohort(item.request)
    if eod:
        from app.models.provider_work import ProviderWork, ProviderWorkAttempt

        replay_identity = db.info.get("provider_work")
        if replay_identity is not None:
            previous = await db.scalar(
                select(ProviderWorkAttempt)
                .join(ProviderWork)
                .where(
                    ProviderWork.work_id == replay_identity["work_id"],
                    ProviderWork.environment == queue.environment(),
                    ProviderWork.provider == provider,
                    ProviderWork.stable_key == f"historical:{item_id}",
                    ProviderWorkAttempt.generation == replay_identity["generation"],
                    ProviderWorkAttempt.token == lease_token,
                    ProviderWorkAttempt.state == "settled",
                )
            )
            if previous is not None:
                if (
                    replay_identity["token"] != lease_token
                    or (previous.proof or {}).get("historical_completion") != completion_report
                    or item.status
                    != (previous.proof or {}).get("historical_item_status", previous.outcome)
                ):
                    raise HistoricalBackfillConflictError("historical completion replay differs")
                return item
    now = utc_now()
    if outcome == "partial" and not eod:
        raise HistoricalBackfillValidationError("partial is restricted to per-member EOD evidence")

    from app.models.provider_work import ProviderWorkOwner
    from app.services.provider_work import environment, fence, prepared_keys

    owner = await db.get(ProviderWorkOwner, (environment(), provider))
    identity = db.info.get("provider_work")
    if identity is None or owner is None:
        raise HistoricalBackfillConflictError("provider work protocol is required")
    owned_work, _, _ = await fence(
        db,
        provider=provider,
        work_id=identity["work_id"],
        generation=identity["generation"],
        token=lease_token,
    )
    if owned_work.kind != "historical" or owned_work.stable_key != f"historical:{item.item_id}":
        raise HistoricalBackfillConflictError("historical completion owner scope differs")
    # An active owner can cross the date boundary before another claim.
    # Apply the domain decision only on this authenticated write path;
    # authorization itself remains a pure observation.
    await expire_requests(db, provider=provider, now=now)
    item.lease_expires_at = owner.expires_at
    if item.request.status == "cancelled" or (
        item.request.status == "failed" and item.status in {"cancelled", "failed"}
    ):
        # Cancellation preserves its terminal decision, while the same
        # fenced owner drains prepared/accepted deliveries. It cannot
        # turn missing members into completed or no-data evidence.
        fence_identity = {
            "provider": provider,
            "work_id": owned_work.work_id,
            "generation": identity["generation"],
            "token": lease_token,
        }
        await queue.close(db, **fence_identity)
        await queue.settle(
            db,
            **fence_identity,
            outcome="failed"
            if outcome == "failed" and failure_code == "PREPARED_DELIVERY_MANUAL_REVIEW"
            else "cancelled",
            failure_code=failure_code
            if failure_code == "PREPARED_DELIVERY_MANUAL_REVIEW"
            else None,
            proof={
                "historical_item_id": str(item_id),
                "cancelled_prepared_drain": True,
                **(
                    {
                        "historical_completion": completion_report,
                        "historical_item_status": item.status,
                    }
                    if eod
                    else {}
                ),
                **(
                    {
                        "manual_pending_prepared": await prepared_keys(db, owned_work.work_id),
                        "manual_reason": "permanent_prepared_delivery_rejection",
                        "source_rejection": failure_message,
                    }
                    if failure_code == "PREPARED_DELIVERY_MANUAL_REVIEW"
                    else {}
                ),
            },
        )
        return item
    if (
        item.request.status == "cancelled"
        or item.status != "running"
        or item.lease_token != lease_token
        or item.lease_expires_at is None
        or item.lease_expires_at <= now
    ):
        raise HistoricalBackfillConflictError("backfill item is not leased")
    run: IngestionRun | None = None
    eod_evidence = None
    if outcome in {"completed", "partial"} and eod:
        try:
            eod_evidence = EODCompletionEvidence.model_validate(result_evidence)
        except ValueError as exc:
            raise HistoricalBackfillValidationError("EOD completion evidence is invalid") from exc
        try:
            cohort = await frozen_cohort(db, item)
        except HistoricalBackfillError as exc:
            raise HistoricalBackfillValidationError(str(exc)) from exc
        if (
            eod_evidence.item_id != item.item_id
            or eod_evidence.request_id != item.request_id
            or eod_evidence.trade_date != item.trade_date
            or not cohort
            or {member.member_key for member in eod_evidence.members}
            != {member["member_key"] for member in cohort["members"]}
            or eod_evidence.schema_version != _contract_version(item.request)
            or eod_evidence.schema_id != _contract_schema(item.request)
            # A no-data member always cites its durable raw observation.
            or any(
                member.outcome == "no_data" and member.raw is None
                for member in eod_evidence.members
            )
            # A dated release freezes only mapped members.
            or (
                member_cohort(item.request)
                and any(member.outcome == "mapping_gap" for member in eod_evidence.members)
            )
        ):
            raise HistoricalBackfillValidationError("EOD evidence differs from the frozen request")
        if member_cohort(item.request) and outcome != (
            "partial" if _eod_partial(eod_evidence) else "completed"
        ):
            raise HistoricalBackfillValidationError("EOD outcome differs from its member evidence")
        item.result_evidence = eod_evidence.model_dump(mode="json")
    elif outcome == "completed" and futures_report_feed(provider, item.request.dataset_key):
        await _validate_futures_completion(db, item, run_id, result_evidence, now)
        item.result_evidence = result_evidence
    elif outcome == "completed":
        if run_id is None:
            raise HistoricalBackfillValidationError("completed item requires a Source run")
        run = await db.get(IngestionRun, run_id, with_for_update=True)
        if not _valid_completed_run(run, item):
            raise HistoricalBackfillValidationError(
                "Source run does not match completed backfill item"
            )
    identity = db.info["provider_work"]
    fence_identity = {
        "provider": provider,
        "work_id": identity["work_id"],
        "generation": identity["generation"],
        "token": lease_token,
    }
    await queue.close(db, **fence_identity)
    db.info["historical_completion_validated"] = identity["work_id"]
    no_data = (
        [
            f"{row.product_code}|{row.contract_code}|{row.session}"
            for row in HistoricalResultEvidence.model_validate(result_evidence).rows
            if row.no_trade
        ]
        if futures_report_feed(provider, item.request.dataset_key) and result_evidence
        else []
    )
    await queue.settle(
        db,
        **fence_identity,
        outcome=outcome,
        proof={
            "historical_item_id": str(item_id),
            **(
                {
                    "historical_completion": completion_report,
                    "historical_item_status": outcome,
                }
                if eod
                else {}
            ),
            "no_data": no_data,
            **({"eod": eod_evidence.model_dump(mode="json")} if eod_evidence else {}),
            **(
                {
                    "manual_pending_prepared": await prepared_keys(db, owned_work.work_id),
                    "manual_reason": "permanent_prepared_delivery_rejection",
                    "source_rejection": failure_message,
                }
                if outcome == "failed" and failure_code == "PREPARED_DELIVERY_MANUAL_REVIEW"
                else {}
            ),
        },
        failure_code=failure_code,
    )
    if eod_evidence is not None:
        from app.services.unlisted_symbols import work_unlisted

        # Writes outside the date's own cohort: supplementary members or unlisted rows.
        item.unlisted_symbols = (
            await work_unlisted(
                db, identity["work_id"], {member.member_key for member in eod_evidence.members}
            )
            or None
        )
    item.status, item.completed_at, item.run_id = outcome, now, run_id
    item.lease_token, item.lease_expires_at = None, None
    item.failure_code = failure_code if outcome == "failed" else None
    item.failure_message = failure_message if outcome == "failed" else None
    request = item.request
    if outcome == "failed":
        request.status, request.completed_at = "failed", now
        request.failure_code, request.failure_message = (
            failure_code or "PROVIDER_FAILED",
            failure_message,
        )
        for sibling in request.items:
            if sibling.status in {"pending", "running"} and sibling.item_id != item.item_id:
                sibling.status, sibling.completed_at = "cancelled", now
                sibling.lease_token, sibling.lease_expires_at = None, None
                await _cancel_queued_item_work(db, sibling, provider=provider, now=now)
    elif all(
        sibling.status in {"completed", "partial", "skipped_calendar"} for sibling in request.items
    ):
        request.status = (
            "partial"
            if any(sibling.status == "partial" for sibling in request.items)
            else "completed"
        )
        request.completed_at = now
    await db.flush()
    from app.services.delivery_plan_summary import refresh_summaries_for_historical_members

    proven: set[str] = set()
    if eod_evidence is not None:
        # Written members already refreshed with their runs; no-data proof has no run.
        proven = {
            member.member_key
            for member in eod_evidence.members
            if member.outcome == "no_data" and member.raw is not None
        }
    elif outcome == "completed" and futures_report_feed(provider, item.request.dataset_key):
        # A futures run's writes count only once this item completes.
        proven = {
            f"{row.product_code}|{row.contract_code}|{row.session}"
            for row in HistoricalResultEvidence.model_validate(result_evidence).rows
        }
    await refresh_summaries_for_historical_members(
        db,
        provider=provider,
        dataset_key=item.request.dataset_key,
        trade_date=item.trade_date,
        member_keys=proven,
    )
    return item


async def validate_eod_historical_delivery(
    db: AsyncSession,
    request: MarketEODIngressRequest | MarketEODIngressRequestV2 | ExchangeDailyQuoteIngressRequest,
) -> None:
    """One frozen-cohort EOD delivery (current or dated release cohort) of a leased date."""
    from app.services.provider_work import fence, prepared_descriptors

    provider = request.source
    if request.delivery is None:
        raise HistoricalBackfillValidationError("historical EOD delivery requires fenced work")
    try:
        item_id = UUID(request.delivery.work_item_id.removeprefix("historical:"))
    except ValueError as exc:
        raise HistoricalBackfillValidationError("historical work identity is invalid") from exc
    item = await db.scalar(
        select(HistoricalBackfillItem)
        .options(selectinload(HistoricalBackfillItem.request))
        .where(HistoricalBackfillItem.item_id == item_id)
    )
    identity = db.info.get("provider_work")
    if item is None or identity is None:
        raise HistoricalBackfillValidationError("historical work is unavailable")
    if item.request.provider != provider or not eod_cohort(item.request):
        raise HistoricalBackfillValidationError("historical delivery is outside its frozen cohort")
    work, _, _ = await fence(
        db, provider=provider, **{key: identity[key] for key in ("work_id", "generation", "token")}
    )
    descriptor = (await prepared_descriptors(db, work.work_id, request.idempotency_key)).get(
        request.idempotency_key
    )
    retained = descriptor is not None and descriptor["body_sha256"] == db.info.get(
        "provider_work_body_sha256"
    )
    now = utc_now()
    try:
        snapshot = await frozen_cohort(db, item) or {}
    except HistoricalBackfillError as exc:
        raise HistoricalBackfillValidationError(str(exc)) from exc
    # Supplementary members are identity-checked like members; only expected differs.
    members = {
        row["symbol"]: row
        for row in (*snapshot.get("members", []), *snapshot.get("supplementary", []))
    }
    unlisted = {row.symbol for row in request.payload.data} - set(members)
    if unlisted:
        from app.services.ingress_contracts import parse_dataset_contract_declaration
        from app.services.unlisted_symbols import unlisted_allowed

        dataset = await db.get(DatasetRegistry, request.dataset_key)
        declaration = parse_dataset_contract_declaration(dataset.config) if dataset else None
        # Served rows outside the cohort are written in the dataset's own currency.
        if (
            declaration is None
            or any(
                row.symbol in unlisted
                and (row.currency or declaration.defaults.currency) != declaration.defaults.currency
                for row in request.payload.data
            )
            or not await unlisted_allowed(
                db, provider=provider, dataset_key=request.dataset_key, symbols=unlisted
            )
        ):
            raise HistoricalBackfillValidationError(
                "historical delivery is outside its frozen cohort"
            )
    # A current-release cohort always states the listing; Full release rows may
    # leave it to the member.
    strict_symbol = current_release_cohort(item.request)
    if (
        work.kind != "historical"
        or work.stable_key != f"historical:{item.item_id}"
        or item.request.dataset_key != request.dataset_key
        or request.schema_version != _contract_version(item.request)
        or (
            not retained
            and (
                item.request.status not in {"queued", "running"}
                or item.status != "running"
                or item.lease_token != identity["token"]
                or item.lease_expires_at is None
                or item.lease_expires_at <= now
            )
        )
        or request.payload.batch.delivery_mode != "backfill"
        or request.payload.batch.data_date != item.trade_date
        or request.payload.batch.coverage_start_date != item.trade_date
        or request.payload.batch.coverage_end_date != item.trade_date
        or request.delivery.target_data_date != item.trade_date
        or request.fetched_at > now + timedelta(minutes=5)
        or not _session_closed(item, request.fetched_at)
        or any(row.trade_date != item.trade_date for row in request.payload.data)
        or any(
            members[row.symbol]["mapping_status"] != "mapped"
            or (strict_symbol or _source_symbol(row) is not None)
            and _source_symbol(row) != members[row.symbol]["provider_symbol"]
            or (strict_symbol or row.currency is not None)
            and row.currency != members[row.symbol]["currency"]
            for row in request.payload.data
            if row.symbol in members
        )
    ):
        raise HistoricalBackfillValidationError("historical delivery is outside its frozen cohort")


def _source_symbol(row) -> str | None:
    """The provider's own symbol; an exchange's quote is keyed by the listed code itself."""
    return getattr(row, "source_symbol", row.symbol)


def _contract_schema(request: HistoricalBackfillRequest) -> str:
    feed = catalog_feed(request.provider, request.dataset_key)
    if feed is None:
        raise HistoricalBackfillValidationError("historical feed is not in the catalog")
    return feed.contract.schema_id


def _contract_version(request: HistoricalBackfillRequest) -> int:
    """Every historical delivery uses its feed's current contract version."""
    feed = catalog_feed(request.provider, request.dataset_key)
    if feed is None:
        raise HistoricalBackfillValidationError("historical feed is not in the catalog")
    return feed.contract.current_version


def _eod_partial(evidence: EODCompletionEvidence) -> bool:
    return any(member.outcome not in {"data", "no_data"} for member in evidence.members)


def _valid_completed_run(run: IngestionRun | None, item: HistoricalBackfillItem) -> bool:
    if run is None or item.request is None:
        return False
    return (
        run.source == item.request.provider
        and run.dataset_key == item.request.dataset_key
        and run.batch_data_date == item.trade_date
        and run.status == "completed"
        and isinstance(run.schema_id, str)
        and run.schema_version is not None
        and isinstance(run.delivery_mode, str)
        and run.total_records == run.success_records
        and run.failed_records == 0
    )


async def _validate_futures_completion(
    db: AsyncSession,
    item: HistoricalBackfillItem,
    run_id: UUID | None,
    evidence: dict | None,
    now: datetime,
) -> None:
    from app.models.raw import RawMarketPayload
    from app.models.registry import DeliveryMemberEvidence

    try:
        proof = HistoricalResultEvidence.model_validate(evidence)
    except ValidationError as exc:
        raise HistoricalBackfillValidationError("futures completion evidence is invalid") from exc
    if (
        proof.item_id != item.item_id
        or proof.request_id != item.request_id
        or proof.trade_date != item.trade_date
        or not futures_report_feed(item.request.provider, item.request.dataset_key)
        or proof.fetched_at > now + timedelta(minutes=5)
        or not _session_closed(item, proof.fetched_at)
    ):
        raise HistoricalBackfillValidationError("futures evidence scope or clock differs")
    data_rows = [row for row in proof.rows if not row.no_trade]
    if not data_rows:
        if run_id is not None:
            raise HistoricalBackfillValidationError(
                "no-data-only completion must not include a run"
            )
        return
    run = await db.get(IngestionRun, run_id, with_for_update=True) if run_id else None
    if not _valid_completed_run(run, item):
        raise HistoricalBackfillValidationError("Source run does not match completed backfill item")
    assert run is not None
    expected_work_item = f"historical:{item.item_id}"
    if (
        run.schema_id != "futures_eod"
        or run.schema_version != _contract_version(item.request)
        or run.delivery_mode != "backfill"
        or run.coverage_start_date != item.trade_date
        or run.coverage_end_date != item.trade_date
        or run.total_records != len(data_rows)
        or (run.metadata_ or {}).get("delivery", {}).get("work_item_id") != expected_work_item
    ):
        raise HistoricalBackfillValidationError("Source run is outside the historical item")
    raw = await db.get(RawMarketPayload, run.raw_payload_id) if run.raw_payload_id else None
    batch = raw.payload.get("batch", {}) if raw is not None else {}
    if (
        raw is None
        or batch.get("source_raw_ref") != proof.manifest.ref
        or batch.get("source_raw_sha256") != proof.manifest.sha256
        or raw.fetched_at != proof.fetched_at
        or raw.payload.get("data") != [row.model_dump(mode="json") for row in data_rows]
    ):
        raise HistoricalBackfillValidationError("Source raw manifest differs from completion proof")
    canonical = (
        await db.scalars(
            select(DeliveryMemberEvidence.member_key).where(
                DeliveryMemberEvidence.run_id == run.run_id
            )
        )
    ).all()
    expected = {f"{row.product_code}|{row.contract_code}|{row.session}" for row in data_rows}
    if set(canonical) != expected:
        raise HistoricalBackfillValidationError("futures canonical member proof is incomplete")


async def validate_futures_historical_delivery(db: AsyncSession, request) -> None:
    """A leased historical item is the authority for this Source-only write path."""
    try:
        item_id = UUID(request.delivery.work_item_id.removeprefix("historical:"))
    except (AttributeError, ValueError) as exc:
        raise HistoricalBackfillValidationError("historical item identity is invalid") from exc
    item = (
        await db.scalars(
            select(HistoricalBackfillItem)
            .options(selectinload(HistoricalBackfillItem.request))
            .where(HistoricalBackfillItem.item_id == item_id)
        )
    ).one_or_none()
    now = utc_now()

    cancelled_prepared = False
    if item is not None:
        from app.models.provider_work import ProviderWork, ProviderWorkOwner
        from app.services.provider_work import environment, fence

        owner = await db.get(ProviderWorkOwner, (environment(), item.request.provider))
        if owner is not None:
            item.lease_expires_at = owner.expires_at
        identity = db.info.get("provider_work")
        if identity and (
            item.request.status == "cancelled"
            or item.request.status == "failed"
            and item.status in {"cancelled", "failed"}
        ):
            from app.services.provider_work import prepared_descriptors

            work = await db.get(ProviderWork, identity["work_id"])
            descriptor = (
                (await prepared_descriptors(db, work.work_id, request.idempotency_key)).get(
                    request.idempotency_key
                )
                if work
                else None
            )
            if (
                work
                and work.kind == "historical"
                and work.stable_key == f"historical:{item.item_id}"
                and descriptor
                and descriptor["body_sha256"] == db.info.get("provider_work_body_sha256")
            ):
                await fence(
                    db,
                    provider=item.request.provider,
                    work_id=work.work_id,
                    generation=identity["generation"],
                    token=identity["token"],
                )
                cancelled_prepared = True
    if item is None or (
        not futures_report_feed(item.request.provider, item.request.dataset_key)
        or item.request.dataset_key != request.dataset_key
        or (
            not cancelled_prepared
            and (
                item.request.status not in {"queued", "running"}
                or item.status != "running"
                or item.lease_expires_at is None
                or item.lease_expires_at <= now
            )
        )
        or request.source != item.request.provider
        or request.schema_id != "futures_eod"
        or request.schema_version != _contract_version(item.request)
        or request.payload.batch.delivery_mode != "backfill"
        or request.payload.batch.data_date != item.trade_date
        or request.payload.batch.coverage_start_date != item.trade_date
        or request.payload.batch.coverage_end_date != item.trade_date
        or request.delivery.target_data_date != item.trade_date
        or any(row.trade_date != item.trade_date for row in request.payload.data)
        or request.fetched_at > now + timedelta(minutes=5)
        or not _session_closed(item, request.fetched_at)
    ):
        raise HistoricalBackfillValidationError("historical delivery is outside its leased scope")
