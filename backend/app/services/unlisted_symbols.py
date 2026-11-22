"""Symbols a whole-market provider served outside the frozen list.

The frozen list measures gaps; it never withholds data the provider served. A
feed whose catalog entry delivers unlisted symbols writes every served row, and
the extras are counted from actual write evidence, never toward ``expected``.
"""

from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_work import ProviderWorkReceipt
from app.models.registry import (
    DailyDeliveryMember,
    DailyDeliveryPart,
    DailyDeliveryPlan,
    DeliveryMemberEvidence,
    IngestionRun,
    UniverseMember,
    UniverseRelease,
)
from app.services.provider_metadata import catalog_feed, delivers_unlisted, provider_catalog


async def listed_elsewhere(
    db: AsyncSession, *, provider: str, dataset_key: str, symbols: set[str]
) -> set[str]:
    """Symbols the provider's latest published list of another asset class names.

    Such a symbol belongs to that dataset; writing it here as unlisted would
    create the instrument under the wrong asset class.
    """
    feed = catalog_feed(provider, dataset_key)
    if feed is None or not symbols:
        return set()
    others = sorted(
        {
            other.dataset_key
            for other in provider_catalog().feeds
            if other.provider == provider
            and other.market == feed.market
            and other.asset_class != feed.asset_class
        }
    )
    if not others:
        return set()
    latest = (
        select(UniverseRelease.release_id)
        .where(
            UniverseRelease.provider == provider,
            UniverseRelease.dataset_key.in_(others),
            UniverseRelease.status == "published",
        )
        .order_by(
            UniverseRelease.dataset_key,
            UniverseRelease.effective_date.desc(),
            UniverseRelease.created_at.desc(),
        )
        .distinct(UniverseRelease.dataset_key)
    )
    return set(
        await db.scalars(
            select(UniverseMember.symbol).where(
                UniverseMember.release_id.in_(latest),
                UniverseMember.symbol.in_(sorted(symbols)),
            )
        )
    )


async def unlisted_allowed(
    db: AsyncSession, *, provider: str, dataset_key: str, symbols: set[str]
) -> bool:
    """Whether these symbols outside the frozen members may be written as unlisted."""
    return delivers_unlisted(provider, dataset_key) and not await listed_elsewhere(
        db, provider=provider, dataset_key=dataset_key, symbols=symbols
    )


async def plan_unlisted(db: AsyncSession, plan: DailyDeliveryPlan) -> list[str]:
    """Symbols outside the plan's members that the plan's terminal runs wrote."""
    if getattr(plan, "supplement_id", None) is None and not delivers_unlisted(
        plan.provider, plan.dataset_key
    ):
        return []
    evidence = DeliveryMemberEvidence
    return list(
        await db.scalars(
            select(evidence.member_key)
            .join(IngestionRun, IngestionRun.run_id == evidence.run_id)
            .join(DailyDeliveryPart, DailyDeliveryPart.part_id == IngestionRun.delivery_part_id)
            .where(
                DailyDeliveryPart.plan_id == plan.plan_id,
                IngestionRun.status.in_(("completed", "completed_with_errors")),
                ~exists().where(
                    DailyDeliveryMember.plan_id == plan.plan_id,
                    DailyDeliveryMember.member_key == evidence.member_key,
                ),
            )
            .distinct()
            .order_by(evidence.member_key)
        )
    )


async def work_unlisted(db: AsyncSession, work_id: UUID, expected: set[str]) -> list[str]:
    """Symbols outside ``expected`` that the work's accepted runs wrote."""
    written = await db.scalars(
        select(DeliveryMemberEvidence.member_key)
        .join(ProviderWorkReceipt, ProviderWorkReceipt.run_id == DeliveryMemberEvidence.run_id)
        .where(ProviderWorkReceipt.work_id == work_id)
        .distinct()
    )
    return sorted(set(written) - expected)
