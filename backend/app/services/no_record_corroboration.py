"""A provider without a record of a member's day, accepted as no trade only when its daily bar agrees.

FinLab publishes no intraday partition for a symbol-day without a board-lot trade,
and also none while that partition is still being built; one answer cannot tell
the two apart. The same provider's daily bar for that day can: either the member
had no trade at all, or the provider served no price for it, so its delivered
bar carries the latest earlier close. A TW price comes only from board-lot trades;
odd-lot trades add volume without a price, so volume cannot tell this apart.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import Instrument, MarketDataEOD
from app.models.registry import (
    DailyDeliveryMember,
    DailyDeliveryPlan,
    DatasetRegistry,
)
from app.provider_catalog import FeedDefinition
from app.services.provider_metadata import acquisition_feeds

# Evidence status of a claim that needs this corroboration; its reason is ``no_trade``.
NO_RECORD = "no_record"
# Markets where a daily price comes only from board-lot trades.
BOARD_LOT_PRICED_MARKETS = frozenset({"TW"})


def _daily_feed(dataset: DatasetRegistry, provider: str) -> FeedDefinition | None:
    """The same provider's daily feed of the same market and asset class, if exactly one."""
    feeds = {feed.dataset_key: feed for feed in acquisition_feeds()}
    intraday = feeds.get(dataset.dataset_key)
    if (
        intraday is None
        or intraday.provider != provider
        or intraday.contract.schema_id != "intraday_partition"
    ):
        return None
    daily = [
        feed
        for feed in feeds.values()
        if feed.provider == provider
        and feed.frequency == "daily"
        and feed.market == intraday.market
        and feed.calendar_market == intraday.calendar_market
        and feed.asset_class == intraday.asset_class
    ]
    return daily[0] if len(daily) == 1 else None


async def corroborate_no_record(
    db: AsyncSession,
    plan: DailyDeliveryPlan,
    member: DailyDeliveryMember,
    dataset: DatasetRegistry,
) -> dict[str, Any] | None:
    """How the daily bar shows the member traded no board lot that day, or ``None``."""
    feed = _daily_feed(dataset, plan.provider)
    if feed is None or feed.market not in BOARD_LOT_PRICED_MARKETS:
        return None
    basis = {"dataset_key": feed.dataset_key}
    # The canonical bar decides first. A delivered bar whose open, high, low and close
    # all equal the latest earlier close is one the provider served without a price:
    # the price was carried, so no board lot traded that day.
    rows = (
        await db.scalars(
            select(MarketDataEOD)
            .join(Instrument, Instrument.instrument_id == MarketDataEOD.instrument_id)
            .where(
                Instrument.market == feed.market,
                Instrument.asset_class == feed.asset_class,
                Instrument.symbol == member.symbol,
                MarketDataEOD.trade_date <= plan.trade_date,
            )
            .order_by(MarketDataEOD.trade_date.desc())
            .limit(2)
        )
    ).all()
    row = rows[0] if rows and rows[0].trade_date == plan.trade_date else None
    if row is not None:
        earlier = rows[1] if len(rows) == 2 else None
        if (
            row.source != plan.provider
            or earlier is None
            or row.close is None
            or earlier.close is None
            or not (row.open == row.high == row.low == row.close == earlier.close)
        ):
            return None
        return {**basis, "basis": "carried_close", "carried_from": earlier.trade_date.isoformat()}
    daily = await db.scalar(
        select(DailyDeliveryMember)
        .join(DailyDeliveryPlan, DailyDeliveryPlan.plan_id == DailyDeliveryMember.plan_id)
        .where(
            DailyDeliveryPlan.dataset_key == feed.dataset_key,
            DailyDeliveryPlan.provider == plan.provider,
            DailyDeliveryPlan.trade_date == plan.trade_date,
            DailyDeliveryPlan.execution_profile == plan.execution_profile,
            DailyDeliveryMember.symbol == member.symbol,
        )
    )
    if daily is None or daily.outcome != "no_data":
        # Not planned, not settled yet, or blocked: the daily bar says nothing yet.
        return None
    return {
        **basis,
        "basis": "daily_no_data",
        "plan_id": str(daily.plan_id),
        "reason": daily.outcome_reason,
    }
