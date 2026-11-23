"""Calendar-required feeds wait for a published exchange calendar, never a weekday default."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select

from app.models.canonical import CalendarRevisionDay
from app.models.registry import DailyDeliveryPlan, DailyExecutionDecision, MissingDeliveryAlert
from app.schemas.full_market import DeliveryPlanCreateRequest
from app.services import delivery_monitor, full_market, historical_backfill
from app.services.full_market import CALENDAR_UNPUBLISHED, FullMarketError, create_plan
from app.services.provider_metadata import calendar_required, provider_catalog
from tests.full_market_fixtures import historical_admission
from tests.test_full_market import DAY, _baseline, _calendar, _dataset


def _require_calendar(monkeypatch, module) -> None:
    monkeypatch.setattr(module, "calendar_required", lambda provider, dataset_key: True)


def test_no_current_feed_requires_a_calendar() -> None:
    assert not any(feed.calendar_required for feed in provider_catalog().feeds)
    assert calendar_required("taifex", "tw_futures_eod") is False
    assert calendar_required("unknown", "tw_futures_eod") is False


async def _plan_body(db) -> tuple[DeliveryPlanCreateRequest, str]:
    dataset = await _dataset(db)
    _, release = await _baseline(db)
    await historical_admission(db, dataset, release, DAY, None, profile="full_market")
    body = DeliveryPlanCreateRequest(
        version=1,
        dataset_key=dataset.dataset_key,
        provider="taifex",
        execution_profile="full_market",
        trade_date=DAY,
        release_id=release.release_id,
    )
    return body, dataset.dataset_key


@pytest.mark.parametrize("offset", [0, 5])
async def test_weekday_default_date_waits_without_freezing_a_decision(
    test_session, monkeypatch, offset
):
    _require_calendar(monkeypatch, full_market)
    body, dataset_key = await _plan_body(test_session)
    # A weekday and a Sunday: neither may be planned nor frozen as a calendar skip.
    body = body.model_copy(update={"trade_date": DAY + timedelta(days=offset)})
    with pytest.raises(FullMarketError) as error:
        await create_plan(test_session, body, allowed_datasets=[dataset_key])
    assert (error.value.status_code, error.value.code) == (409, CALENDAR_UNPUBLISHED)
    assert await test_session.scalar(select(func.count()).select_from(DailyDeliveryPlan)) == 0
    assert await test_session.scalar(select(func.count()).select_from(DailyExecutionDecision)) == 0


async def test_published_calendar_date_plans_normally(test_session, monkeypatch):
    _require_calendar(monkeypatch, full_market)
    body, dataset_key = await _plan_body(test_session)
    await _calendar(test_session, open_dates=[DAY])
    plan = await create_plan(test_session, body, allowed_datasets=[dataset_key])
    assert plan.status != "skipped_calendar"
    decision = await test_session.scalar(select(DailyExecutionDecision))
    assert decision.kind == "planned"
    assert decision.snapshot["calendar_source"] == "published"


async def test_source_exposes_the_calendar_wait_code(client, test_session, monkeypatch):
    from tests.test_full_market import _activated_plan, _source_headers

    _, _, first = await _activated_plan(test_session)
    _require_calendar(monkeypatch, full_market)
    headers = await _source_headers(test_session)
    body = {
        "version": 1,
        "provider": "taifex",
        "dataset_key": "tw_futures_eod",
        # Outside the published fixture year, so the date has only a weekday default.
        "trade_date": "2027-07-20",
        "release_id": str(first.release_id),
    }
    response = await client.post("/api/v1/source/delivery-plans", headers=headers, json=body)
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": CALENDAR_UNPUBLISHED}}


async def test_backfill_refuses_ranges_with_unpublished_dates(test_session, monkeypatch):
    from tests.test_historical_backfill import _seed_scope

    _require_calendar(monkeypatch, historical_backfill)
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    missing = today - timedelta(days=1)
    await test_session.execute(
        delete(CalendarRevisionDay).where(CalendarRevisionDay.trade_date == missing)
    )
    market, reason, days = await historical_backfill.preview_request(
        test_session,
        provider="finlab",
        dataset_key="tw_equity_eod",
        start_date=missing,
        end_date=today,
        today=today,
    )
    assert (market, reason) == ("TW", "calendar_unpublished")
    assert [(valid, day_reason) for _, valid, day_reason in days] == [
        (False, "calendar_unpublished"),
        (False, "calendar_unpublished"),
    ]
    with pytest.raises(historical_backfill.HistoricalBackfillError, match="2026-08-31"):
        await historical_backfill.create_request(
            test_session,
            provider="finlab",
            dataset_key="tw_equity_eod",
            start_date=missing,
            end_date=today,
            request_key="calendar-required-gap",
            created_by="operator",
            today=today,
        )
    # The published day alone is a valid range.
    _, reason, days = await historical_backfill.preview_request(
        test_session,
        provider="finlab",
        dataset_key="tw_equity_eod",
        start_date=today,
        end_date=today,
        today=today,
    )
    assert reason is None
    assert [(valid, day_reason) for _, valid, day_reason in days] == [(True, None)]


async def test_overdue_unplanned_date_reports_the_unpublished_calendar(test_session, monkeypatch):
    _require_calendar(monkeypatch, delivery_monitor)
    dataset = await _dataset(test_session)
    _, baseline = await _baseline(test_session, effective_date=DAY - timedelta(days=1))
    await historical_admission(test_session, dataset, baseline, DAY, None)
    await test_session.commit()

    # TAIFEX starts 17:00 Taipei and is due 12 hours later, 21:00 UTC.
    evaluated_at = datetime(2026, 7, 21, 21, 1, tzinfo=timezone.utc)
    await delivery_monitor.scan_missing_deliveries(test_session, now=evaluated_at)

    alert = await test_session.scalar(
        select(MissingDeliveryAlert).where(MissingDeliveryAlert.expected_data_date == DAY)
    )
    assert alert is not None
    assert alert.details["code"] == "CALENDAR_UNPUBLISHED"
    assert alert.details["calendar_market"] == "TAIFEX"
    assert alert.details["completion_deadline_at"] == "2026-07-21T21:00:00+00:00"
