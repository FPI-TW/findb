"""Durable, provider-neutral historical backfill control plane.

The backend only validates and leases work.  Fetcher owns credentials and
acquisition, then submits data through the normal raw-first Source ingest API.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.canonical import CalendarRevisionDay, CalendarYearRevision
from app.models.registry import (
    DatasetRegistry,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
    IngestionRun,
    SchedulerControl,
    SchedulerDataset,
)
from app.utils import utc_now, uuid7

CAPABILITIES: dict[tuple[str, str], str] = {
    ("twelve_data", "us_equity_eod"): "declared",
    ("finlab", "tw_equity_eod"): "declared",
    ("shioaji", "tw_equity_minute"): "executable",
    ("shioaji", "tw_etf_minute"): "executable",
}
MAX_BACKFILL_DAYS = 31
LEASE_DURATION = timedelta(minutes=15)
PROVIDER_TIMEZONES: dict[str, str] = {
    "twelve_data": "America/New_York",
    "finlab": "Asia/Taipei",
    "shioaji": "Asia/Taipei",
}


class HistoricalBackfillError(ValueError):
    pass


class HistoricalBackfillConflictError(HistoricalBackfillError):
    pass


class HistoricalBackfillValidationError(HistoricalBackfillError):
    pass


def _provider_current_date(provider: str, now: datetime) -> date:
    """Return the provider's market date for the same cutoff as its Fetcher."""
    timezone_name = PROVIDER_TIMEZONES.get(provider)
    if timezone_name is None:
        raise HistoricalBackfillError("provider timezone is not configured")
    if now.tzinfo is None:
        raise HistoricalBackfillError("historical claim clock must be timezone-aware")
    return now.astimezone(ZoneInfo(timezone_name)).date()


async def _open_dates(db: AsyncSession, market: str, start: date, end: date) -> list[date]:
    rows = (
        await db.execute(
            select(CalendarRevisionDay.trade_date, CalendarRevisionDay.is_open)
            .join(
                CalendarYearRevision,
                CalendarRevisionDay.calendar_revision_id == CalendarYearRevision.id,
            )
            .where(
                CalendarYearRevision.market == market,
                CalendarYearRevision.status == "published",
                CalendarRevisionDay.trade_date >= start,
                CalendarRevisionDay.trade_date <= end,
            )
            .order_by(CalendarRevisionDay.trade_date)
        )
    ).all()
    # A gap makes the calendar unsafe: every requested date must be explicitly
    # published. Published market closures are intentionally omitted from the
    # executable item list while preserving the operator's requested range.
    for year in range(start.year, end.year + 1):
        revision = (
            await db.execute(
                select(CalendarYearRevision.id).where(
                    CalendarYearRevision.market == market,
                    CalendarYearRevision.year == year,
                    CalendarYearRevision.status == "published",
                )
            )
        ).scalar_one_or_none()
        if revision is None:
            raise HistoricalBackfillError(f"published calendar is required for {market} {year}")
    published_days = {trade_date: is_open for trade_date, is_open in rows}
    requested_days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    if any(value not in published_days for value in requested_days):
        raise HistoricalBackfillError("requested dates include unpublished calendar days")
    return [value for value in requested_days if published_days[value] is True]


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
    today = today or utc_now().date()
    if start_date < today - timedelta(days=MAX_BACKFILL_DAYS - 1) or end_date > today:
        raise HistoricalBackfillError("backfill dates must be within the latest 31 calendar days")
    if (end_date - start_date).days >= MAX_BACKFILL_DAYS:
        raise HistoricalBackfillError("backfill range exceeds 31 days")
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
    if (provider, dataset_key) not in CAPABILITIES:
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
    dates = await _open_dates(db, dataset.market, start_date, end_date)
    if not dates:
        raise HistoricalBackfillError("requested range has no open market dates")
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
    )
    row.items = [
        HistoricalBackfillItem(item_id=uuid7(), trade_date=trade_date, status="pending")
        for trade_date in dates
    ]
    db.add(row)
    await db.flush()
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


async def list_enabled_scopes(db: AsyncSession) -> list[tuple[str, str, str, bool]]:
    """Expose only configured active scopes; no credential information leaks."""
    rows: list[tuple[str, str, str, bool]] = []
    for (provider, dataset_key), capability in CAPABILITIES.items():
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
    today = today or utc_now().date()
    dataset = await db.get(DatasetRegistry, dataset_key)
    market = dataset.market if dataset is not None else None
    scope_reason: str | None = None
    if end_date < start_date:
        scope_reason = "date_order_invalid"
    elif start_date < today - timedelta(days=MAX_BACKFILL_DAYS - 1) or end_date > today:
        scope_reason = "outside_latest_31_days"
    elif dataset is None or not dataset.is_active:
        scope_reason = "dataset_inactive"
    elif (provider, dataset_key) not in CAPABILITIES:
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
    calendar_rows: dict[date, bool] = {}
    if market is not None:
        result = await db.execute(
            select(CalendarRevisionDay.trade_date, CalendarRevisionDay.is_open)
            .join(
                CalendarYearRevision,
                CalendarRevisionDay.calendar_revision_id == CalendarYearRevision.id,
            )
            .where(
                CalendarYearRevision.market == market,
                CalendarYearRevision.status == "published",
                CalendarRevisionDay.trade_date >= start_date,
                CalendarRevisionDay.trade_date <= end_date,
            )
        )
        calendar_rows = {trade_date: is_open for trade_date, is_open in result.all()}
    days: list[tuple[date, bool, str | None]] = []
    current = start_date
    while current <= end_date:
        if scope_reason is not None:
            days.append((current, False, scope_reason))
        elif current not in calendar_rows:
            days.append((current, False, "calendar_unpublished"))
        elif not calendar_rows[current]:
            days.append((current, False, "market_closed"))
        else:
            days.append((current, True, None))
        current += timedelta(days=1)
    return market, scope_reason, days


async def cancel_request(
    db: AsyncSession, request_id: UUID, *, actor: str
) -> HistoricalBackfillRequest:
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
    if row.status in {"completed", "failed", "cancelled"}:
        raise HistoricalBackfillConflictError("backfill request is already terminal")
    now = utc_now()
    row.status, row.cancelled_by, row.cancelled_at = "cancelled", actor, now
    for item in row.items:
        if item.status in {"pending", "running"}:
            item.status, item.completed_at = "cancelled", now
            item.lease_token, item.lease_expires_at = None, None
    await db.flush()
    return row


async def claim_next_item(
    db: AsyncSession,
    *,
    provider: str,
    allowed_datasets: list[str] | None,
    now: datetime | None = None,
) -> HistoricalBackfillItem | None:
    """Lease one date only when its request has no earlier unfinished item."""
    now = now or utc_now()
    expiry_date = _provider_current_date(provider, now) - timedelta(days=MAX_BACKFILL_DAYS)
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
    # A worker crash or a failed completion call must not leave the serial
    # request permanently blocked. Tokens make any expired worker's late
    # completion harmless after this safe reclaim.
    await db.execute(
        update(HistoricalBackfillItem)
        .where(
            HistoricalBackfillItem.status == "running",
            HistoricalBackfillItem.lease_expires_at.is_not(None),
            HistoricalBackfillItem.lease_expires_at <= now,
        )
        .values(status="pending", lease_token=None, lease_expires_at=None)
    )
    stmt = (
        select(HistoricalBackfillItem)
        .join(HistoricalBackfillRequest)
        .options(selectinload(HistoricalBackfillItem.request))
        .where(
            HistoricalBackfillRequest.provider == provider,
            HistoricalBackfillRequest.status.in_(("queued", "running")),
            HistoricalBackfillItem.status == "pending",
        )
        .order_by(HistoricalBackfillRequest.created_at, HistoricalBackfillItem.trade_date)
        .with_for_update(skip_locked=True)
    )
    if allowed_datasets is not None:
        stmt = stmt.where(HistoricalBackfillRequest.dataset_key.in_(allowed_datasets))
    for item in (await db.execute(stmt)).scalars().unique():
        prior = (
            await db.execute(
                select(HistoricalBackfillItem.item_id)
                .where(
                    HistoricalBackfillItem.request_id == item.request_id,
                    HistoricalBackfillItem.trade_date < item.trade_date,
                    HistoricalBackfillItem.status != "completed",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if prior is not None:
            continue
        item.status, item.started_at = "running", now
        item.lease_token = uuid7()
        item.lease_expires_at = now + LEASE_DURATION
        item.lease_attempt += 1
        item.request.status = "running"
        item.request.started_at = item.request.started_at or now
        await db.flush()
        return item
    return None


async def complete_item(
    db: AsyncSession,
    item_id: UUID,
    *,
    provider: str,
    outcome: Literal["completed", "failed"],
    lease_token: UUID,
    run_id: UUID | None,
    failure_code: str | None,
    failure_message: str | None,
) -> HistoricalBackfillItem:
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
    now = utc_now()
    if (
        item.request.status == "cancelled"
        or item.status != "running"
        or item.lease_token != lease_token
        or item.lease_expires_at is None
        or item.lease_expires_at <= now
    ):
        raise HistoricalBackfillConflictError("backfill item is not leased")
    run: IngestionRun | None = None
    if outcome == "completed":
        if run_id is None:
            raise HistoricalBackfillValidationError("completed item requires a Source run")
        run = await db.get(IngestionRun, run_id, with_for_update=True)
        if not _valid_completed_run(run, item):
            raise HistoricalBackfillValidationError(
                "Source run does not match completed backfill item"
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
            if sibling.status == "pending":
                sibling.status, sibling.completed_at = "cancelled", now
    elif all(sibling.status == "completed" for sibling in request.items):
        request.status, request.completed_at = "completed", now
    await db.flush()
    return item


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
