"""A new listing that never traded is unavailable once its no-data retries end (#593)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.canonical import Instrument, MarketDataEOD
from app.models.registry import DailyDeliveryMember, DailyDeliveryPlan
from app.schemas.full_market import DeliveryOutcomeRequest
from app.services import full_market
from app.services.full_market import plan_complete, reconcile_plan
from app.utils import utc_now
from tests.test_full_market import DAY, _activated_plan
from tests.test_full_market_lateness import _block


async def _plan(db):
    _, body, plan = await _activated_plan(db, "tw_equity_eod", count=2)
    members = (
        await db.scalars(
            select(DailyDeliveryMember)
            .where(DailyDeliveryMember.plan_id == plan.plan_id)
            .order_by(DailyDeliveryMember.member_key)
        )
    ).all()
    return body.provider, plan, members


async def _summary(db, plan):
    return await reconcile_plan(db, await db.get(DailyDeliveryPlan, plan.plan_id))


def _state(summary, key: str) -> tuple[str, str | None]:
    gap = next(gap for gap in summary.gaps if gap["member_key"] == key)
    return gap["status"], gap["reason"]


async def _traded(db, symbol: str, day) -> None:
    instrument = Instrument(asset_class="equity", market="TW", symbol=symbol)
    db.add(instrument)
    await db.flush()
    db.add(
        MarketDataEOD(
            instrument_id=instrument.instrument_id,
            trade_date=day,
            open=Decimal("10"),
            high=Decimal("10"),
            low=Decimal("10"),
            close=Decimal("10"),
            volume=1000,
            source="finlab",
            asof_ts=utc_now(),
        )
    )
    await db.flush()


@pytest.mark.asyncio
async def test_a_new_listing_without_rows_is_unavailable_after_its_last_no_data_retry(
    test_session,
):
    provider, plan, (new, other) = await _plan(test_session)
    await _block(test_session, plan, provider, new, "source_error")
    # A retry still to come: the member stays blocked and holds the plan open.
    assert _state(await _summary(test_session, plan), new.member_key) == (
        "blocked",
        "source_error",
    )

    await _block(test_session, plan, provider, new, "no_data_exhausted")
    await _block(test_session, plan, provider, other, "mapping_gap")

    summary = await _summary(test_session, plan)
    assert _state(summary, new.member_key) == ("unavailable", "not_yet_trading")
    assert (summary.blocked, summary.missing, summary.unavailable, summary.mapping_gap) == (
        0,
        0,
        2,
        1,
    )
    assert plan_complete(summary)


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["traded_before", "listed_long_ago"])
async def test_an_exhausted_member_with_history_stays_blocked(test_session, monkeypatch, history):
    provider, plan, (member, _) = await _plan(test_session)
    if history == "traded_before":
        await _traded(test_session, member.member_key, DAY - timedelta(days=1))
    else:
        monkeypatch.setattr(full_market, "NOT_YET_TRADING_PLAN_DAYS", 0)

    await _block(test_session, plan, provider, member, "no_data_exhausted")

    assert _state(await _summary(test_session, plan), member.member_key) == (
        "blocked",
        "no_data_exhausted",
    )


@pytest.mark.asyncio
async def test_a_row_after_the_trade_date_does_not_reclassify_the_day(test_session):
    provider, plan, (member, _) = await _plan(test_session)
    await _traded(test_session, member.member_key, DAY + timedelta(days=1))

    await _block(test_session, plan, provider, member, "no_data_exhausted")

    assert _state(await _summary(test_session, plan), member.member_key) == (
        "unavailable",
        "not_yet_trading",
    )


def test_no_data_exhausted_is_a_blocked_reason_only():
    body = {"version": 1, "work_item_id": "w", "member_key": "m", "reason": "no_data_exhausted"}
    assert DeliveryOutcomeRequest(**body, outcome="blocked").reason == "no_data_exhausted"
    with pytest.raises(ValueError):
        DeliveryOutcomeRequest(**body, outcome="no_data")
