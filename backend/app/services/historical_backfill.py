"""Durable, provider-neutral historical backfill control plane.

The backend only validates and leases work.  Fetcher owns credentials and
acquisition, then submits data through the normal raw-first Source ingest API.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.registry import (
    DatasetRegistry,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
    IngestionRun,
    SchedulerControl,
    SchedulerDataset,
)
from app.services.effective_calendar import effective_days, execution_policy
from app.utils import utc_now, uuid7

CAPABILITIES: dict[tuple[str, str], str] = {
    ("twelve_data", "us_equity_eod"): "declared",
    ("finlab", "tw_equity_eod"): "declared",
    ("shioaji", "tw_equity_minute"): "executable",
    ("shioaji", "tw_etf_minute"): "executable",
}
STAGING_MAX_BACKFILL_DAYS = 31
PRODUCTION_MAX_BACKFILL_DAYS = 366
# Compatibility for existing staging callers; production uses the target-aware helper.
MAX_BACKFILL_DAYS = STAGING_MAX_BACKFILL_DAYS
LEASE_DURATION = timedelta(minutes=15)
PROVIDER_TIMEZONES: dict[str, str] = {
    "twelve_data": "America/New_York",
    "finlab": "Asia/Taipei",
    "shioaji": "Asia/Taipei",
}


def _max_backfill_days() -> int:
    return (
        PRODUCTION_MAX_BACKFILL_DAYS
        if os.getenv("APP_ENVIRONMENT", "staging").strip().lower() == "production"
        else STAGING_MAX_BACKFILL_DAYS
    )


def _supports_backfill(provider: str, dataset_key: str) -> bool:
    if (
        os.getenv("APP_ENVIRONMENT", "staging").strip().lower() == "production"
        and provider == "shioaji"
    ):
        return False
    return (provider, dataset_key) in CAPABILITIES


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


def _session_closed(item: HistoricalBackfillItem, now: datetime) -> bool:
    """Use the immutable request snapshot for new acquisition, never live calendar data."""
    snapshot = item.calendar_snapshot
    if now.tzinfo is None:
        raise HistoricalBackfillError("historical acquisition clock must be timezone-aware")
    if not isinstance(snapshot, dict) or snapshot.get("trade_date") != item.trade_date.isoformat():
        raise HistoricalBackfillError("historical calendar snapshot is invalid")
    try:
        zone = ZoneInfo(snapshot["timezone"])
        close = time.fromisoformat(snapshot["regular_close"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoricalBackfillError("historical calendar snapshot is invalid") from exc
    return now.astimezone(zone) >= datetime.combine(item.trade_date, close, zone)


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
    max_days = _max_backfill_days()
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
    calendar = await effective_days(db, dataset.market, start_date, end_date)
    policy = execution_policy(provider)
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
    today = today or utc_now().date()
    dataset = await db.get(DatasetRegistry, dataset_key)
    market = dataset.market if dataset is not None else None
    scope_reason: str | None = None
    if end_date < start_date:
        scope_reason = "date_order_invalid"
    elif start_date < today - timedelta(days=_max_backfill_days() - 1) or end_date > today:
        scope_reason = f"outside_latest_{_max_backfill_days()}_days"
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
            date.fromisoformat(day["trade_date"]): day["is_open"]
            for day in await effective_days(db, market, start_date, end_date)
        }
        if market is not None
        else {}
    )
    skip_non_trading = (
        execution_policy(provider)["skip_non_trading_days"]
        if provider in {"twelve_data", "finlab", "shioaji", "taifex"}
        else False
    )
    days: list[tuple[date, bool, str | None]] = []
    current = start_date
    while current <= end_date:
        if scope_reason is not None:
            days.append((current, False, scope_reason))
        elif skip_non_trading and not calendar_rows[current]:
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
    expiry_date = _provider_current_date(provider, now) - timedelta(days=_max_backfill_days())
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
        if not _session_closed(item, now):
            continue
        prior = (
            await db.execute(
                select(HistoricalBackfillItem.item_id)
                .where(
                    HistoricalBackfillItem.request_id == item.request_id,
                    HistoricalBackfillItem.trade_date < item.trade_date,
                    HistoricalBackfillItem.status.not_in(("completed", "skipped_calendar")),
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
    allowed = bool(
        request.status not in {"cancelled", "completed", "failed"}
        and item.status == "running"
        and item.lease_token == lease_token
        and item.lease_expires_at is not None
        and item.lease_expires_at > checked_at
        and _session_closed(item, checked_at)
    )
    return (
        allowed,
        checked_at,
        item.lease_expires_at if allowed else None,
        request.request_id,
        request.dataset_key,
    )


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
    elif all(sibling.status in {"completed", "skipped_calendar"} for sibling in request.items):
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
