"""A provider's missing intraday record is no trade only when the same day's daily bar agrees."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from app.models.canonical import CalendarYearRevision, Instrument, MarketDataEOD
from app.models.registry import DailyDeliveryMember, SchedulerControl
from app.schemas.full_market import DeliveryOutcomeRequest, DeliveryPlanCreateRequest
from app.services.full_market import create_plan, record_outcome
from app.utils import utc_now
from tests.full_market_fixtures import historical_admission
from tests.intraday_support import DAY
from tests.test_full_market import _baseline, _dataset
from tests.test_intraday_partition_plan import _plan

EVIDENCE_URL = "https://www.finlab.tw/data/intraday?dataset=tw_equity_bar_1m"


def _claim(work_item_id: str, member_key: str, *, status: str = "no_record", reason="no_trade"):
    return {
        "version": 1,
        "work_item_id": work_item_id,
        "member_key": member_key,
        "outcome": "no_data",
        "reason": reason,
        "evidence": {
            "url": EVIDENCE_URL,
            "sha256": "a" * 64,
            "observed_at": utc_now().isoformat(),
            "source_symbol": member_key,
            "trade_date": DAY.isoformat(),
            "session": None,
            "source_status": status,
            "source_excerpt": '{"manifest":{"date":"2026-07-21","status":"absent"}}',
        },
    }


async def _plans(db):
    """An intraday plan and the same day's daily plan, both of FinLab, members 0001 and 0002."""
    _, intraday, headers = await _plan(db)
    # A later start admits the daily dataset alongside the intraday one.
    control = await db.get(SchedulerControl, "full_market_finlab_v1")
    control.revision += 1
    await db.commit()
    daily_dataset = await _dataset(db, "tw_equity_eod")
    _, release = await _baseline(db, "tw_equity_eod", 2)
    revision = await db.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == "TW")
    )
    await historical_admission(db, daily_dataset, release, DAY, revision.id)
    daily = await create_plan(
        db,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key="tw_equity_eod",
            provider="finlab",
            trade_date=DAY,
            release_id=release.release_id,
        ),
        allowed_datasets=["tw_equity_eod"],
    )
    return intraday, daily, headers


async def _member(db, plan, symbol: str) -> DailyDeliveryMember:
    return (
        await db.scalars(
            select(DailyDeliveryMember).where(
                DailyDeliveryMember.plan_id == plan.plan_id, DailyDeliveryMember.symbol == symbol
            )
        )
    ).one()


CARRIED = ("10", "10", "10", "10")


async def _daily_bar(
    db,
    symbol: str,
    bar: tuple[str, str, str, str] = CARRIED,
    *,
    previous_close: str | None = "10",
    volume: int = 1616,
    source: str = "finlab",
) -> None:
    """The member's delivered daily bar of the day, after its bar of the previous day."""
    instrument = Instrument(asset_class="equity", market="TW", symbol=symbol)
    db.add(instrument)
    await db.flush()
    days = [(DAY, bar, volume, source)]
    if previous_close is not None:
        days.append((DAY - timedelta(days=1), (previous_close,) * 4, 5000, "finlab"))
    for day, (open_, high, low, close), shares, origin in days:
        db.add(
            MarketDataEOD(
                instrument_id=instrument.instrument_id,
                trade_date=day,
                open=Decimal(open_),
                high=Decimal(high),
                low=Decimal(low),
                close=Decimal(close),
                volume=shares,
                source=origin,
                asof_ts=utc_now(),
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_a_daily_no_trade_corroborates_a_missing_intraday_record(
    owned_client: AsyncClient, test_session
) -> None:
    intraday, daily, headers = await _plans(test_session)
    await record_outcome(
        test_session,
        daily.plan_id,
        DeliveryOutcomeRequest.model_validate(
            _claim(daily.parts[0].work_item_id, "0001", status="no_trade")
        ),
        provider="finlab",
        allowed_datasets=["tw_equity_eod"],
    )

    response = await owned_client.post(
        f"/api/v1/source/delivery-plans/{intraday.plan_id}/outcomes",
        headers=headers,
        json=_claim(intraday.parts[0].work_item_id, "0001"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["no_data"] == 1
    test_session.expire_all()
    member = await _member(test_session, intraday, "0001")
    assert (member.outcome, member.outcome_reason) == ("no_data", "no_trade")
    assert member.outcome_evidence["source_status"] == "no_record"
    # The corroborating daily outcome is kept with the claim it settled.
    assert member.outcome_history[-1]["corroboration"] == {
        "dataset_key": "tw_equity_eod",
        "basis": "daily_no_data",
        "plan_id": str(daily.plan_id),
        "reason": "no_trade",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "daily",
    [
        # The daily member is not settled yet: nothing is known about the day.
        None,
        # FinLab served a price: a board lot traded, so its partition should exist.
        {"bar": ("10", "10.5", "9.9", "10.2")},
        # One price all day, but a new one: a board-lot trade, not a carried close.
        {"bar": ("10.3", "10.3", "10.3", "10.3")},
        # Fewer than 1,000 shares no longer settle it: FinLab still priced the day.
        {"bar": ("10.3", "10.3", "10.3", "10.3"), "volume": 300},
        # Nothing earlier to have carried a close from.
        {"previous_close": None},
        # The carried shape, but the bar is not FinLab's own.
        {"source": "twelve_data"},
    ],
    ids=[
        "daily pending",
        "priced",
        "flat at a new price",
        "priced below one lot",
        "no earlier bar",
        "another provider's bar",
    ],
)
async def test_a_missing_record_without_corroboration_is_refused_and_stays_retryable(
    owned_client: AsyncClient, test_session, daily
) -> None:
    intraday, _, headers = await _plans(test_session)
    if daily is not None:
        await _daily_bar(test_session, "0001", **daily)

    response = await owned_client.post(
        f"/api/v1/source/delivery-plans/{intraday.plan_id}/outcomes",
        headers=headers,
        json=_claim(intraday.parts[0].work_item_id, "0001"),
    )

    assert response.status_code == 409, response.text
    # The exact code is what tells the Fetcher to keep the member retryable.
    assert response.json() == {"detail": {"code": "no_data_not_corroborated"}}
    test_session.expire_all()
    member = await _member(test_session, intraday, "0001")
    assert (member.outcome, member.outcome_history) == (None, [])


@pytest.mark.asyncio
async def test_a_daily_bar_carrying_the_earlier_close_corroborates_a_missing_record(
    owned_client: AsyncClient, test_session
) -> None:
    intraday, _, headers = await _plans(test_session)
    # FinLab served no price that day, only odd-lot volume above one lot; the bar
    # was delivered with the previous close.
    await _daily_bar(test_session, "0002", volume=1616)

    response = await owned_client.post(
        f"/api/v1/source/delivery-plans/{intraday.plan_id}/outcomes",
        headers=headers,
        json=_claim(intraday.parts[0].work_item_id, "0002"),
    )

    assert response.status_code == 200, response.text
    test_session.expire_all()
    member = await _member(test_session, intraday, "0002")
    assert member.outcome == "no_data"
    assert member.outcome_history[-1]["corroboration"] == {
        "dataset_key": "tw_equity_eod",
        "basis": "carried_close",
        "carried_from": (DAY - timedelta(days=1)).isoformat(),
    }


@pytest.mark.asyncio
async def test_only_an_intraday_dataset_may_claim_a_missing_record(test_session) -> None:
    from app.services.full_market import FullMarketError

    _, daily, _ = await _plans(test_session)

    with pytest.raises(FullMarketError) as refused:
        await record_outcome(
            test_session,
            daily.plan_id,
            DeliveryOutcomeRequest.model_validate(_claim(daily.parts[0].work_item_id, "0001")),
            provider="finlab",
            allowed_datasets=["tw_equity_eod"],
        )

    assert refused.value.code == "no_data_not_corroborated"


def test_a_missing_record_can_only_mean_no_trade() -> None:
    with pytest.raises(ValidationError, match="source_status must match reason"):
        DeliveryOutcomeRequest.model_validate(_claim("fp1:x", "0001", reason="halted"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason, bar, counted",
    [
        # The day had only odd lots: FinLab's daily bar carries the earlier close.
        ("empty_response", CARRIED, True),
        # A board lot traded at a new price: its partition should exist.
        ("empty_response", ("10.3", "10.3", "10.3", "10.3"), False),
        # Another blocking reason is not a missing record at all.
        ("source_error", CARRIED, False),
    ],
    ids=["carried close", "priced", "provider error"],
)
async def test_a_member_left_blocked_for_an_empty_answer_counts_once_the_bar_shows_no_trade(
    test_session, reason, bar, counted
) -> None:
    """Members blocked before #612 (odd lots above one lot) reclassify, outcomes untouched."""
    from app.models.registry import DailyDeliveryPlanSummary
    from app.services.delivery_plan_summary import refresh_plan_summary

    intraday, _, _ = await _plans(test_session)
    await record_outcome(
        test_session,
        intraday.plan_id,
        DeliveryOutcomeRequest.model_validate(
            {
                "version": 1,
                "work_item_id": intraday.parts[0].work_item_id,
                "member_key": "0002",
                "outcome": "blocked",
                "reason": reason,
            }
        ),
        provider="finlab",
        allowed_datasets=["tw_equity_bar_1m"],
    )
    await _daily_bar(test_session, "0002", bar)
    await refresh_plan_summary(test_session, intraday.plan_id)
    summary = await test_session.get(
        DailyDeliveryPlanSummary, intraday.plan_id, populate_existing=True
    )
    assert (summary.no_data_count, summary.blocked_count) == ((1, 0) if counted else (0, 1))
    test_session.expire_all()
    member = await _member(test_session, intraday, "0002")
    assert (member.outcome, member.outcome_reason) == ("blocked", reason)
