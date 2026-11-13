"""Owner-requested universe refreshes: one queued maintenance work that carries approval."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminPrincipal
from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import DatasetRegistry, UniverseRelease
from app.services import provider_work
from app.services.source_clients import PROVIDER_DATASET_SCOPE
from app.services.universe_queries import present_universe_release
from app.utils import utc_now, uuid7

# Only the Admin API writes this scope key; Source enqueue rejects it.
MANUAL_REFRESH = "manual_refresh"
# TAIFEX universes are dated per trade date from its daily report, not refreshed from a listing.
_DATED_UNIVERSE_PROVIDERS = frozenset({"taifex"})


class UniverseRefreshError(ValueError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def refreshable_feeds() -> list[tuple[str, str]]:
    """Provider and dataset pairs whose universe a maintenance work refreshes."""
    return sorted(
        (provider, dataset_key)
        for provider, dataset_keys in PROVIDER_DATASET_SCOPE.items()
        if provider not in _DATED_UNIVERSE_PROVIDERS
        for dataset_key in dataset_keys
    )


def _manual_works(provider: str, dataset_key: str):
    return select(ProviderWork).where(
        ProviderWork.environment == provider_work.environment(),
        ProviderWork.provider == provider,
        ProviderWork.dataset_key == dataset_key,
        ProviderWork.kind == "maintenance",
        ProviderWork.scope.bool_op("?")(MANUAL_REFRESH),
    )


async def request_refresh(
    db: AsyncSession,
    *,
    provider: str,
    dataset_key: str,
    note: str,
    principal: AdminPrincipal,
) -> ProviderWork:
    """Queue one refresh whose resulting release is published as approved by ``principal``."""
    provider = provider.strip().lower()
    if (provider, dataset_key) not in refreshable_feeds():
        raise UniverseRefreshError("Universe refresh is not available for this feed", 404)
    if await db.get(DatasetRegistry, dataset_key) is None:
        raise UniverseRefreshError("Dataset is not registered", 404)
    # The provider lock serializes requests, so two clicks cannot both pass the check.
    await provider_work.lock_provider(db, provider)
    pending = await db.scalar(
        _manual_works(provider, dataset_key).where(ProviderWork.completed_at.is_(None)).limit(1)
    )
    if pending is not None:
        raise UniverseRefreshError("A manual universe refresh is already in progress", 409)
    return await provider_work.enqueue(
        db,
        provider=provider,
        stable_key=f"maintenance:{dataset_key}:manual:{uuid7()}",
        kind="maintenance",
        dataset_key=dataset_key,
        trade_date=None,
        scope={
            "dataset_key": dataset_key,
            MANUAL_REFRESH: {
                "requested_by": str(principal),
                "actor_type": principal.actor_type,
                "actor_id": principal.actor_id,
                "display_name": principal.display_name,
                "requested_at": utc_now().isoformat(),
                "note": note,
            },
        },
    )


async def owning_refresh(
    db: AsyncSession, *, provider: str, dataset_key: str
) -> ProviderWork | None:
    """The manual refresh that holds the provider's owner slot right now, if any.

    A provider has one owner at a time, so a universe submitted while this work is
    open comes from it. Nothing a Source client sends can assert the approval.
    """
    return await db.scalar(
        _manual_works(provider, dataset_key)
        .join(ProviderWorkAttempt, ProviderWorkAttempt.work_id == ProviderWork.work_id)
        .join(ProviderWorkOwner, ProviderWorkOwner.attempt_id == ProviderWorkAttempt.attempt_id)
        .where(
            ProviderWorkOwner.environment == provider_work.environment(),
            ProviderWorkOwner.provider == provider,
            ProviderWork.state == "running",
            ProviderWorkAttempt.state == "open",
        )
    )


def present_refresh(work: ProviderWork, attempt: ProviderWorkAttempt | None = None) -> dict:
    request = work.scope[MANUAL_REFRESH]
    return {
        "work_id": work.work_id,
        "provider": work.provider,
        "dataset_key": work.dataset_key,
        "state": work.state,
        "requested_by": request["display_name"],
        "requested_at": request["requested_at"],
        "note": request["note"],
        "completed_at": work.completed_at,
        "failure_code": attempt.failure_code if attempt is not None else None,
    }


async def list_feeds(db: AsyncSession) -> list[dict[str, Any]]:
    """Each refreshable feed with its published release, open candidates and last request."""
    rows = []
    for provider, dataset_key in refreshable_feeds():
        releases = select(UniverseRelease).where(
            UniverseRelease.dataset_key == dataset_key, UniverseRelease.provider == provider
        )
        published = await db.scalar(
            releases.where(UniverseRelease.status == "published")
            .order_by(UniverseRelease.effective_date.desc(), UniverseRelease.created_at.desc())
            .limit(1)
        )
        candidate = await db.scalar(
            releases.where(UniverseRelease.status == "candidate")
            .order_by(UniverseRelease.effective_date.desc(), UniverseRelease.created_at.desc())
            .limit(1)
        )
        # A candidate older than the published release was superseded, not left waiting.
        if (
            candidate is not None
            and published is not None
            and (candidate.effective_date, candidate.created_at)
            < (published.effective_date, published.created_at)
        ):
            candidate = None
        work = await db.scalar(
            _manual_works(provider, dataset_key).order_by(ProviderWork.created_at.desc()).limit(1)
        )
        attempt = (
            None
            if work is None
            else await db.scalar(
                select(ProviderWorkAttempt)
                .where(ProviderWorkAttempt.work_id == work.work_id)
                .order_by(ProviderWorkAttempt.attempt_number.desc())
                .limit(1)
            )
        )
        rows.append(
            {
                "provider": provider,
                "dataset_key": dataset_key,
                "published": None if published is None else present_universe_release(published),
                "candidate": None if candidate is None else present_universe_release(candidate),
                "refresh": None if work is None else present_refresh(work, attempt),
            }
        )
    return rows
