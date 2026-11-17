"""A provider without a record of a member's day, accepted as no trade only when its daily bar agrees.

FinLab publishes no intraday partition for a symbol-day without a board-lot trade,
and also none while that partition is still being built; one answer cannot tell
the two apart. The same provider's daily bar for that day can: the member either
had no trade at all, or traded less than one board lot.
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
# Shares in one board lot. Daily volume is in shares, so less than this is odd lots only.
BOARD_LOT_SHARES = {"TW": 1000}


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
    lot = BOARD_LOT_SHARES.get(feed.market) if feed is not None else None
    if feed is None or lot is None:
        return None
    basis = {"dataset_key": feed.dataset_key, "board_lot_shares": lot}
    # The canonical bar decides first: a member with a row traded, and only its volume
    # can show that the trade was odd lots only.
    row = await db.scalar(
        select(MarketDataEOD)
        .join(Instrument, Instrument.instrument_id == MarketDataEOD.instrument_id)
        .where(
            Instrument.market == feed.market,
            Instrument.asset_class == feed.asset_class,
            Instrument.symbol == member.symbol,
            MarketDataEOD.trade_date == plan.trade_date,
        )
    )
    if row is not None:
        if row.source != plan.provider or row.volume is None or row.volume >= lot:
            return None
        return {**basis, "basis": "odd_lots_only", "volume_shares": row.volume}
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
