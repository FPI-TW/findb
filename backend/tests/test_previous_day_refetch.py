"""An index feed's next daily plan queues a backfill of its previous trading day (#680)."""

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select, update

from app.models.registry import DailyDeliveryPlan, HistoricalBackfillRequest, SchedulerDataset
from app.schemas.full_market import DeliveryPlanCreateRequest
from app.services import historical_backfill, previous_day_refetch
from app.services.full_market import create_plan
from tests.test_full_market import DAY, _activated_plan

NEXT = DAY + timedelta(days=1)


async def _next_plan(db, key, body, trade_date=NEXT):
    return await create_plan(
        db,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=key,
            provider=body.provider,
            trade_date=trade_date,
            release_id=(await db.scalar(select(DailyDeliveryPlan.release_id).limit(1))),
        ),
        allowed_datasets=[key],
    )


async def _created(db, plan_id, hours_after_midnight: float) -> None:
    """Pretend the plan was created on its own schedule, this long after its date began.

    It also binds the feed to the FMP control, as production does, which a backfill requires.
    """
    plan = await db.get(DailyDeliveryPlan, plan_id)
    if await db.get(SchedulerDataset, ("full_market_fmp_v1", plan.dataset_key)) is None:
        db.add(SchedulerDataset(scheduler_key="full_market_fmp_v1", dataset_key=plan.dataset_key))
    midnight = datetime.combine(DAY, time.min, tzinfo=timezone.utc)
    await db.execute(
        update(DailyDeliveryPlan)
        .where(DailyDeliveryPlan.plan_id == plan_id)
        .values(created_at=midnight + timedelta(hours=hours_after_midnight))
    )
    await db.commit()


async def _requests(db) -> list[HistoricalBackfillRequest]:
    return list(await db.scalars(select(HistoricalBackfillRequest)))


def _today(monkeypatch, day=NEXT) -> None:
    monkeypatch.setattr(historical_backfill, "_request_today", lambda provider, dataset: day)


async def test_the_next_plan_queues_the_previous_trading_day(test_session, monkeypatch):
    _, body, first = await _activated_plan(test_session, "us_index_eod", open_dates=[DAY, NEXT])
    # The first plan of a feed has no previous day to refetch.
    assert await _requests(test_session) == []
    # The daily 08:30 Taipei run of the next day: about 24 hours after DAY began.
    await _created(test_session, first.plan_id, 24.5)
    _today(monkeypatch)

    await _next_plan(test_session, "us_index_eod", body)

    [request] = await _requests(test_session)
    assert (request.provider, request.dataset_key) == ("fmp", "us_index_eod")
    assert (request.start_date, request.end_date) == (DAY, DAY)
    assert request.request_key == f"previous-day-refetch:us_index_eod:{DAY.isoformat()}"
    assert request.created_by == previous_day_refetch.CREATED_BY
    await test_session.refresh(request, ["items"])
    assert [(item.trade_date, item.status) for item in request.items] == [(DAY, "pending")]

    # The key is fixed per date: queuing again returns the same request.
    plan = await test_session.get(DailyDeliveryPlan, (await _next_plan_id(test_session)))
    again = await previous_day_refetch.schedule_previous_day_refetch(test_session, plan)
    assert again is not None and again.request_id == request.request_id
    assert len(await _requests(test_session)) == 1


async def _next_plan_id(db):
    return await db.scalar(
        select(DailyDeliveryPlan.plan_id).where(DailyDeliveryPlan.trade_date == NEXT)
    )


async def test_the_previous_plan_is_the_previous_trading_day_not_the_calendar_day(
    test_session, monkeypatch
):
    # A closed day in between (a holiday or weekend) has no plan of its own.
    later = DAY + timedelta(days=3)
    _, body, first = await _activated_plan(test_session, "hk_index_eod", open_dates=[DAY, later])
    await _created(test_session, first.plan_id, 12)
    _today(monkeypatch, later)

    await _next_plan(test_session, "hk_index_eod", body, trade_date=later)

    [request] = await _requests(test_session)
    assert (request.dataset_key, request.start_date) == ("hk_index_eod", DAY)


async def test_a_catch_up_plan_and_other_feeds_queue_nothing(test_session, monkeypatch):
    _, body, first = await _activated_plan(test_session, "us_index_eod", open_dates=[DAY, NEXT])
    # Created days after its date (a restart catching up): already revised when fetched.
    await _created(test_session, first.plan_id, 24 * 3)
    _today(monkeypatch)
    await _next_plan(test_session, "us_index_eod", body)
    assert await _requests(test_session) == []

    # The dollar index (and any feed not listed) is never refetched.
    assert ("fmp", "us_dollar_index_eod") not in previous_day_refetch.REFETCH_FEEDS
    assert ("taifex", "tw_futures_eod") not in previous_day_refetch.REFETCH_FEEDS


async def test_a_refused_backfill_never_fails_the_daily_plan(test_session, monkeypatch, caplog):
    _, body, first = await _activated_plan(test_session, "us_index_eod", open_dates=[DAY, NEXT])
    await _created(test_session, first.plan_id, 24.5)

    async def refused(*_args, **_kwargs):
        raise historical_backfill.HistoricalBackfillError("outside the backfill window")

    monkeypatch.setattr(historical_backfill, "create_request", refused)
    plan = await _next_plan(test_session, "us_index_eod", body)

    assert plan.trade_date == NEXT
    assert await _requests(test_session) == []
    assert "previous_day_refetch=failed dataset=us_index_eod" in caplog.text
    assert "outside the backfill window" not in caplog.text
