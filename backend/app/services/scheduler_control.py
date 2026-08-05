"""Database control-plane operations for provider-owned schedulers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminPrincipal
from app.models.registry import SchedulerControl
from app.services.admin_audit import record_admin_audit
from app.utils import ensure_utc, utc_now


class SchedulerControlError(Exception):
    """Base error for scheduler control-plane operations."""


class SchedulerControlNotFoundError(SchedulerControlError):
    """The requested scheduler key is not registered."""


class SchedulerControlRevisionConflictError(SchedulerControlError):
    """The caller attempted to mutate an outdated scheduler revision."""


class SchedulerControlScopeError(SchedulerControlError):
    """The authenticated Source credential is outside the scheduler scope."""


# Compatibility aliases for callers that use shorter names.
SchedulerNotFoundError = SchedulerControlNotFoundError
SchedulerRevisionConflictError = SchedulerControlRevisionConflictError
SchedulerScopeError = SchedulerControlScopeError


def _normalize_timestamp(value: datetime | None) -> datetime | None:
    return ensure_utc(value) if value is not None else None


async def list_scheduler_controls(db: AsyncSession) -> list[SchedulerControl]:
    """Return all registered schedulers in stable key order."""
    result = await db.execute(select(SchedulerControl).order_by(SchedulerControl.scheduler_key))
    return list(result.scalars().all())


async def get_scheduler_control(db: AsyncSession, scheduler_key: str) -> SchedulerControl | None:
    """Load one scheduler without acquiring a mutation lock."""
    return await db.get(SchedulerControl, scheduler_key)


async def update_scheduler_desired_state(
    db: AsyncSession,
    *,
    scheduler_key: str,
    desired_state: str,
    expected_revision: int,
    principal: AdminPrincipal,
    commit: bool = True,
) -> SchedulerControl:
    """Atomically update desired state and append its Admin audit event."""
    result = await db.execute(
        select(SchedulerControl)
        .where(SchedulerControl.scheduler_key == scheduler_key)
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise SchedulerControlNotFoundError(scheduler_key)
    if row.revision != expected_revision:
        raise SchedulerControlRevisionConflictError(
            f"scheduler revision is {row.revision}, expected {expected_revision}"
        )

    row.desired_state = desired_state
    row.revision += 1
    row.updated_at = utc_now()
    await record_admin_audit(
        db,
        principal,
        action="update",
        resource_type="scheduler_control",
        resource_id=row.scheduler_key,
        details={
            "desired_state": row.desired_state,
            "expected_revision": expected_revision,
            "revision": row.revision,
        },
        commit=False,
    )
    if commit:
        await db.commit()
        await db.refresh(row)
    return row


def _source_scope(db: AsyncSession) -> tuple[str | None, list[str] | None]:
    info: dict[str, Any] = db.info if isinstance(db.info, dict) else {}
    source_name = info.get("source_name")
    allowed_datasets = info.get("allowed_datasets")
    if source_name is not None and not isinstance(source_name, str):
        source_name = str(source_name)
    if allowed_datasets is not None and not isinstance(allowed_datasets, list):
        # A malformed credential scope must fail closed rather than becoming
        # equivalent to a provider-wide (NULL) scope.
        return source_name, ["__invalid_scope__"]
    return source_name, allowed_datasets


def _check_source_scope(db: AsyncSession, row: SchedulerControl) -> None:
    source_name, allowed_datasets = _source_scope(db)
    provider_matches = (
        isinstance(source_name, str) and source_name.strip().lower() == row.provider.strip().lower()
    )
    datasets_match = allowed_datasets is None or all(
        dataset_key in allowed_datasets for dataset_key in row.dataset_keys
    )
    if not provider_matches or not datasets_match:
        raise SchedulerControlScopeError("Source credential is not authorized for this scheduler")


async def poll_scheduler_control(
    db: AsyncSession,
    *,
    scheduler_key: str,
    observed_state: str,
    cycle_started_at: datetime | None = None,
    cycle_completed_at: datetime | None = None,
    last_error: str | None = None,
    commit: bool = True,
) -> tuple[SchedulerControl, datetime]:
    """Validate provider scope and persist one Source heartbeat report."""
    row = await get_scheduler_control(db, scheduler_key)
    if row is None:
        raise SchedulerControlNotFoundError(scheduler_key)
    _check_source_scope(db, row)

    server_time = utc_now()
    row.observed_state = observed_state
    row.last_heartbeat_at = server_time
    if cycle_started_at is not None:
        row.last_cycle_started_at = _normalize_timestamp(cycle_started_at)
    if cycle_completed_at is not None:
        row.last_cycle_completed_at = _normalize_timestamp(cycle_completed_at)
        # A completed cycle with no error is the explicit success signal that
        # clears an older failure. Idle/long-cycle heartbeats preserve it.
        row.last_error = last_error
    elif last_error is not None:
        row.last_error = last_error
    row.updated_at = server_time
    if commit:
        await db.commit()
        await db.refresh(row)
    return row, server_time


def present_scheduler_control(
    row: SchedulerControl, *, now: datetime | None = None
) -> dict[str, Any]:
    """Shape a scheduler ORM row for the Admin API, including heartbeat age."""
    reference = ensure_utc(now) if now is not None else utc_now()
    heartbeat = _normalize_timestamp(row.last_heartbeat_at)
    heartbeat_age = max(0.0, (reference - heartbeat).total_seconds()) if heartbeat else None
    return {
        "scheduler_key": row.scheduler_key,
        "provider": row.provider,
        "dataset_keys": list(row.dataset_keys),
        "desired_state": row.desired_state,
        "observed_state": row.observed_state,
        "revision": row.revision,
        "last_heartbeat_at": heartbeat,
        "last_cycle_started_at": _normalize_timestamp(row.last_cycle_started_at),
        "last_cycle_completed_at": _normalize_timestamp(row.last_cycle_completed_at),
        "last_error": row.last_error,
        "created_at": _normalize_timestamp(row.created_at),
        "updated_at": _normalize_timestamp(row.updated_at),
        "heartbeat_age_seconds": heartbeat_age,
    }


__all__ = [
    "SchedulerControlError",
    "SchedulerControlNotFoundError",
    "SchedulerControlRevisionConflictError",
    "SchedulerControlScopeError",
    "SchedulerNotFoundError",
    "SchedulerRevisionConflictError",
    "SchedulerScopeError",
    "get_scheduler_control",
    "list_scheduler_controls",
    "poll_scheduler_control",
    "present_scheduler_control",
    "update_scheduler_desired_state",
]
