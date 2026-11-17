"""A provider's missing intraday record is no trade only when the same day's daily bar agrees."""

from __future__ import annotations

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


async def _daily_bar(db, symbol: str, volume: int, *, source: str = "finlab") -> None:
    instrument = Instrument(asset_class="equity", market="TW", symbol=symbol)
    db.add(instrument)
    await db.flush()
    db.add(
        MarketDataEOD(
            instrument_id=instrument.instrument_id,
            trade_date=DAY,
            open=Decimal("10"),
            high=Decimal("10"),
            low=Decimal("10"),
            close=Decimal("10"),
            volume=volume,
            source=source,
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
        "board_lot_shares": 1000,
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
        # A board lot traded, so FinLab's partition should exist.
        {"volume": 1000},
        # Odd lots only, but the bar is not FinLab's own.
        {"volume": 300, "source": "twelve_data"},
    ],
    ids=["daily pending", "board lot traded", "another provider's bar"],
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
async def test_odd_lots_only_on_the_daily_bar_corroborate_a_missing_record(
    owned_client: AsyncClient, test_session
) -> None:
    intraday, _, headers = await _plans(test_session)
    await _daily_bar(test_session, "0002", 999)

    response = await owned_client.post(
        f"/api/v1/source/delivery-plans/{intraday.plan_id}/outcomes",
        headers=headers,
        json=_claim(intraday.parts[0].work_item_id, "0002"),
    )

    assert response.status_code == 200, response.text
    test_session.expire_all()
    member = await _member(test_session, intraday, "0002")
    assert member.outcome == "no_data"
    assert member.outcome_history[-1]["corroboration"]["basis"] == "odd_lots_only"


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
