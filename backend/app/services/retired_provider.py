"""Closed provider work of a retired provider, for purging Fetcher's local copies.

The Fetcher journal has no provider column and Source answers only for the
caller's own credential scope, so a retired provider's local rows are found by
the work IDs listed here. Backend records are never changed.
"""

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import SchedulerControl
from app.services.provider_metadata import provider_catalog
from app.utils import utc_now

TERMINAL_WORK_STATES = frozenset({"completed", "partial", "failed", "cancelled"})


class RetiredProviderError(RuntimeError):
    """The provider is not retired, or some of its work is not settled."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons


def work_ids_digest(work_ids: list[str]) -> str:
    """SHA-256 of the sorted IDs, one per line; the Fetcher purge checks the same digest."""
    return hashlib.sha256(
        "".join(f"{work_id}\n" for work_id in sorted(work_ids)).encode()
    ).hexdigest()


async def retired_provider_works(db: AsyncSession, provider: str) -> dict[str, Any]:
    """Every work ID of ``provider`` in this environment, once nothing of it can run again."""
    environment = get_settings().APP_ENVIRONMENT
    reasons: list[str] = []
    if any(entry.provider == provider for entry in provider_catalog().providers):
        reasons.append("provider_in_catalog")
    controls = (
        await db.scalars(select(SchedulerControl).where(SchedulerControl.provider == provider))
    ).all()
    if any(control.retired_at is None for control in controls):
        reasons.append("control_not_retired")
    works = (
        await db.execute(
            select(ProviderWork.work_id, ProviderWork.state).where(
                ProviderWork.environment == environment, ProviderWork.provider == provider
            )
        )
    ).all()
    if any(state not in TERMINAL_WORK_STATES for _, state in works):
        reasons.append("work_not_terminal")
    unsettled = await db.scalar(
        select(ProviderWorkAttempt.attempt_id)
        .join(ProviderWork, ProviderWork.work_id == ProviderWorkAttempt.work_id)
        .where(
            ProviderWork.environment == environment,
            ProviderWork.provider == provider,
            ProviderWorkAttempt.state != "settled",
        )
        .limit(1)
    )
    if unsettled is not None:
        reasons.append("attempt_not_settled")
    owner = await db.get(ProviderWorkOwner, {"environment": environment, "provider": provider})
    if owner is not None and owner.attempt_id is not None:
        reasons.append("owner_holds_attempt")
    if reasons:
        raise RetiredProviderError(reasons)
    work_ids = sorted(str(work_id) for work_id, _ in works)
    return {
        "version": 1,
        "provider": provider,
        "environment": environment,
        "generated_at": utc_now().isoformat(),
        "work_ids": work_ids,
        "sha256": work_ids_digest(work_ids),
    }
