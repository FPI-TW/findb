"""Re-acquire the previous trading day once a feed's provider has revised it (#680).

FMP publishes index rows with a preliminary volume (0 for ^HSI and ^HSCE, about
half of ^GSPC's) and fills or corrects it within about a day; closes do not change.
A daily plan cannot deliver another date, and the previous day's work is closed by
then, so creating the next day's plan also queues an ordinary historical backfill
of the previous plan's trade date. The historical consumer re-acquires it like any
backfill and the newer fetch replaces the row; a preliminary 0 never replaces a
filled volume (``index_quality.zero_volume_keeps_stored``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import DailyDeliveryPlan, HistoricalBackfillRequest

logger = logging.getLogger(__name__)

# (provider, dataset_key) of feeds whose previous trading day is re-acquired. The
# dollar index is not: it trades almost around the clock, and FMP replaces the
# day's row with the next session within hours.
REFETCH_FEEDS: frozenset[tuple[str, str]] = frozenset(
    {("fmp", "hk_index_eod"), ("fmp", "us_index_eod")}
)
CREATED_BY = "system:previous-day-refetch"
# A previous plan created this long after its trade date was itself a catch-up:
# the provider had already revised the day, so there is nothing to refetch.
FRESH_PLAN_AGE = timedelta(hours=48)


def request_key(dataset_key: str, trade_date) -> str:
    return f"previous-day-refetch:{dataset_key}:{trade_date.isoformat()}"


async def previous_plan(db: AsyncSession, plan: DailyDeliveryPlan) -> DailyDeliveryPlan | None:
    """The same feed's latest plan before this one: the previous trading day."""
    return await db.scalar(
        select(DailyDeliveryPlan)
        .where(
            DailyDeliveryPlan.dataset_key == plan.dataset_key,
            DailyDeliveryPlan.provider == plan.provider,
            DailyDeliveryPlan.execution_profile == plan.execution_profile,
            DailyDeliveryPlan.trade_date < plan.trade_date,
        )
        .order_by(DailyDeliveryPlan.trade_date.desc())
        .limit(1)
    )


def _fresh(plan: DailyDeliveryPlan) -> bool:
    midnight = datetime.combine(plan.trade_date, time.min, tzinfo=UTC)
    return plan.created_at - midnight <= FRESH_PLAN_AGE


async def schedule_previous_day_refetch(
    db: AsyncSession, plan: DailyDeliveryPlan
) -> HistoricalBackfillRequest | None:
    """Queue the previous trading day's backfill; never fails the daily plan.

    Runs after the plan is committed, in its own transaction. The request key is
    fixed per date, so a repeated call returns the existing request.
    """
    # A rollback expires the plan, so its identity is read once, up front.
    provider, dataset_key, trade_date = plan.provider, plan.dataset_key, plan.trade_date
    if (provider, dataset_key) not in REFETCH_FEEDS or plan.execution_profile != "full_market":
        return None
    from app.services.historical_backfill import HistoricalBackfillError, create_request

    try:
        previous = await previous_plan(db, plan)
        if previous is None or not _fresh(previous):
            return None
        previous_date = previous.trade_date
        request, duplicate = await create_request(
            db,
            provider=provider,
            dataset_key=dataset_key,
            start_date=previous_date,
            end_date=previous_date,
            request_key=request_key(dataset_key, previous_date),
            created_by=CREATED_BY,
        )
        request_id = request.request_id
        await db.commit()
    except (HistoricalBackfillError, SQLAlchemyError) as exc:
        await db.rollback()
        # Only bounded identifiers: the day's own delivery is unaffected.
        logger.warning(
            "previous_day_refetch=failed dataset=%s trade_date=%s reason=%s",
            dataset_key,
            trade_date.isoformat(),
            type(exc).__name__,
        )
        return None
    if not duplicate:
        logger.info(
            "previous_day_refetch=queued dataset=%s trade_date=%s request=%s",
            dataset_key,
            previous_date.isoformat(),
            request_id,
        )
    return request
