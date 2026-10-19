"""Durable, provider-neutral historical backfill control plane.

The backend only validates and leases work.  Fetcher owns credentials and
acquisition, then submits data through the normal raw-first Source ingest API.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError
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
from app.schemas.historical_backfill import HistoricalResultEvidence
from app.services.effective_calendar import effective_days, execution_policy
from app.utils import utc_now, uuid7

CAPABILITIES: dict[tuple[str, str], str] = {
    ("twelve_data", "us_equity_eod"): "declared",
    ("finlab", "tw_equity_eod"): "declared",
    ("shioaji", "tw_equity_minute"): "executable",
    ("shioaji", "tw_etf_minute"): "executable",
    ("taifex", "tw_futures_eod"): "executable",
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
    "taifex": "Asia/Taipei",
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


def _historical_policy(provider: str) -> dict:
    policy = execution_policy(provider)
    if provider == "taifex":
        # Official daily reports do not exist for exchange-closed dates.
        policy = {key: value for key, value in policy.items() if key != "sha256"}
        policy["skip_non_trading_days"] = True
        policy["sha256"] = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return policy


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


def expiry_cutoff(provider: str, now: datetime) -> date:
    """One read-only provider-local boundary for claims and live acquisition."""
    return _provider_current_date(provider, now) - timedelta(
        days=_max_backfill_days() - (1 if provider == "taifex" else 0)
    )


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
    if item.request.provider == "taifex":
        close = max(close, time(17))
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
    today = today or (
        _provider_current_date(provider, utc_now()) if provider == "taifex" else utc_now().date()
    )
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
    calendar = await effective_days(
        db, "TAIFEX" if provider == "taifex" else dataset.market, start_date, end_date
    )
    policy = _historical_policy(provider)
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
    from app.services.provider_work import enqueue

    for item in row.items:
        if item.status != "pending":
            continue
        snapshot = item.calendar_snapshot
        assert snapshot is not None
        close = time.fromisoformat(snapshot["regular_close"])
        if provider == "taifex":
            close = max(close, time(17))
        await enqueue(
            db,
            provider=provider,
            stable_key=f"historical:{item.item_id}",
            kind="historical",
            dataset_key=dataset_key,
            trade_date=item.trade_date,
            scope={"item_id": str(item.item_id), "request_id": str(row.request_id)},
            not_before=datetime.combine(item.trade_date, close, ZoneInfo(snapshot["timezone"])),
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
    today = today or (
        _provider_current_date(provider, utc_now()) if provider == "taifex" else utc_now().date()
    )
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
            for day in await effective_days(
                db, "TAIFEX" if provider == "taifex" else market, start_date, end_date
            )
        }
        if market is not None
        else {}
    )
    skip_non_trading = (
        _historical_policy(provider)["skip_non_trading_days"]
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


async def _cancel_queued_item_work(
    db: AsyncSession, item: HistoricalBackfillItem, *, provider: str, now: datetime
) -> None:
    """Project a terminal domain decision without abandoning retained deliveries.

    Callers hold the provider transaction lock before locking request/item rows.
    """
    from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkReceipt
    from app.services.provider_work import environment

    work = await db.scalar(
        select(ProviderWork).where(
            ProviderWork.environment == environment(),
            ProviderWork.provider == provider,
            ProviderWork.stable_key == f"historical:{item.item_id}",
        )
    )
    if work is None or work.state != "queued":
        return
    retained = bool(work.prepared_deliveries) or bool(
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
    if row.status in {"completed", "failed", "cancelled"}:
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
    expiry_date = expiry_cutoff(provider, now)
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
    """Lease one date only when its request has no earlier unfinished item."""
    now = now or utc_now()
    from app.config import get_settings

    if get_settings().PROVIDER_WORK_REQUIRED:
        from app.services.provider_work import ProviderWorkError, fence, lock_provider

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

        retained = bool(work.prepared_deliveries) or bool(
            await db.scalar(
                select(ProviderWorkReceipt.run_id)
                .where(ProviderWorkReceipt.work_id == work.work_id)
                .limit(1)
            )
        )
        cancelled_prepared = bool(
            item and item.request.status in {"cancelled", "failed"} and retained
        )
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
    from app.services.provider_work import lock_provider

    await lock_provider(db, provider)
    if provider == "taifex":
        if not await _taifex_maintenance_window(db):
            return None
        in_flight = list(
            await db.scalars(
                select(IngestionRun).where(
                    IngestionRun.source == "taifex",
                    IngestionRun.dataset_key == "tw_futures_eod",
                    IngestionRun.status.in_(("queued", "pending", "processing", "retrying")),
                )
            )
        )
    else:
        in_flight = []
    await expire_requests(db, provider=provider, now=now)
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
        if any(
            (run.metadata_ or {}).get("delivery", {}).get("work_item_id")
            != f"historical:{item.item_id}"
            for run in in_flight
        ):
            continue
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
        and item.trade_date >= expiry_cutoff(provider, checked_at)
        and _session_closed(item, checked_at)
        and (provider != "taifex" or await _taifex_maintenance_window(db))
    )
    from app.config import get_settings

    projected_expiry = item.lease_expires_at
    if get_settings().PROVIDER_WORK_REQUIRED:
        from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
        from app.services.provider_work import environment

        owner = await db.get(ProviderWorkOwner, (environment(), provider), populate_existing=True)
        attempt = (
            await db.get(ProviderWorkAttempt, owner.attempt_id)
            if owner and owner.attempt_id
            else None
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
            and item.trade_date >= expiry_cutoff(provider, checked_at)
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
    outcome: Literal["completed", "failed"],
    lease_token: UUID,
    run_id: UUID | None,
    failure_code: str | None,
    failure_message: str | None,
    result_evidence: dict | None = None,
) -> HistoricalBackfillItem:
    from app.config import get_settings
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
    now = utc_now()

    if get_settings().PROVIDER_WORK_REQUIRED:
        from app.models.provider_work import ProviderWorkOwner
        from app.services.provider_work import environment, fence

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
                            "manual_pending_prepared": list(owned_work.prepared_deliveries),
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
    if outcome == "completed" and provider == "taifex":
        await _validate_taifex_completion(db, item, run_id, result_evidence, now)
        item.result_evidence = result_evidence
    elif outcome == "completed":
        if run_id is None:
            raise HistoricalBackfillValidationError("completed item requires a Source run")
        run = await db.get(IngestionRun, run_id, with_for_update=True)
        if not _valid_completed_run(run, item):
            raise HistoricalBackfillValidationError(
                "Source run does not match completed backfill item"
            )
    if get_settings().PROVIDER_WORK_REQUIRED:
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
            if provider == "taifex" and result_evidence
            else []
        )
        await queue.settle(
            db,
            **fence_identity,
            outcome=outcome,
            proof={
                "historical_item_id": str(item_id),
                "no_data": no_data,
                **(
                    {
                        "manual_pending_prepared": list(owned_work.prepared_deliveries),
                        "manual_reason": "permanent_prepared_delivery_rejection",
                        "source_rejection": failure_message,
                    }
                    if outcome == "failed" and failure_code == "PREPARED_DELIVERY_MANUAL_REVIEW"
                    else {}
                ),
            },
            failure_code=failure_code,
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


async def _taifex_maintenance_window(db: AsyncSession) -> bool:
    controls = (
        await db.scalars(
            select(SchedulerControl)
            .where(SchedulerControl.provider == "taifex", SchedulerControl.retired_at.is_(None))
            .execution_options(populate_existing=True)
        )
    ).all()
    return bool(controls) and all(
        control.desired_state == "stopped" and control.observed_state == "stopped"
        for control in controls
    )


async def _validate_taifex_completion(
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
        raise HistoricalBackfillValidationError("TAIFEX completion evidence is invalid") from exc
    if (
        proof.item_id != item.item_id
        or proof.request_id != item.request_id
        or proof.trade_date != item.trade_date
        or item.request.dataset_key != "tw_futures_eod"
        or proof.fetched_at > now + timedelta(minutes=5)
        or not _session_closed(item, proof.fetched_at)
    ):
        raise HistoricalBackfillValidationError("TAIFEX evidence scope or clock differs")
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
        or run.schema_version != 1
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
        raise HistoricalBackfillValidationError("TAIFEX canonical member proof is incomplete")


async def validate_taifex_historical_delivery(db: AsyncSession, request) -> None:
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
    from app.config import get_settings

    cancelled_prepared = False
    if get_settings().PROVIDER_WORK_REQUIRED and item is not None:
        from app.models.provider_work import ProviderWork, ProviderWorkOwner
        from app.services.provider_work import environment, fence

        owner = await db.get(ProviderWorkOwner, (environment(), "taifex"))
        if owner is not None:
            item.lease_expires_at = owner.expires_at
        identity = db.info.get("provider_work")
        if identity and (
            item.request.status == "cancelled"
            or item.request.status == "failed"
            and item.status in {"cancelled", "failed"}
        ):
            work = await db.get(ProviderWork, identity["work_id"])
            descriptor = (
                (work.prepared_deliveries or {}).get(request.idempotency_key) if work else None
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
                    provider="taifex",
                    work_id=work.work_id,
                    generation=identity["generation"],
                    token=identity["token"],
                )
                cancelled_prepared = True
    if item is None or (
        item.request.provider != "taifex"
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
        or request.source != "taifex"
        or request.schema_id != "futures_eod"
        or request.schema_version != 1
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
