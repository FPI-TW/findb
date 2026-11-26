"""Completeness matrix: every cell state from stored counts, at a fixed statement count."""

from datetime import datetime, time, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.models.registry import (
    DailyDeliveryPart,
    DailyDeliveryPlan,
    DailyDeliveryPlanSummary,
    DailyExecutionDecision,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
    MissingDeliveryAlert,
)
from app.services.feed_schedule import latest_due_date
from app.utils import utc_now, uuid7
from tests.query_budget import assert_within_budget, count_statements
from tests.test_admin_overview_query_budget import _environment

URL = "/api/v1/admin/completeness"
DATASET = "tw_equity_eod"


async def _plan(db, day):
    return await db.scalar(
        select(DailyDeliveryPlan).where(
            DailyDeliveryPlan.dataset_key == DATASET, DailyDeliveryPlan.trade_date == day
        )
    )


def _weekdays(start, end):
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


@pytest.mark.asyncio
async def test_cells_cover_every_state_from_stored_counts(
    client: AsyncClient, test_session, admin_headers: dict
):
    db = test_session
    await _environment(db, ["finlab"], days=14)
    now = utc_now()
    today = now.date()
    first = today - timedelta(days=14)
    through = latest_due_date(DATASET, now)
    complete_day, pending_day, unsynced_day, unplanned_day = list(
        _weekdays(first, through - timedelta(days=1))
    )[:4]

    complete = await _plan(db, complete_day)
    summary = await db.get(DailyDeliveryPlanSummary, complete.plan_id)
    summary.data_count, summary.missing_count = 2, 0
    part = await db.scalar(
        select(DailyDeliveryPart).where(DailyDeliveryPart.plan_id == complete.plan_id)
    )
    part.data_count, part.missing_count = 2, 0
    pending = await _plan(db, pending_day)
    pending.deadline_at = now + timedelta(hours=2)
    unsynced = await _plan(db, unsynced_day)
    stale = await db.get(DailyDeliveryPlanSummary, unsynced.plan_id)
    stale.parts_synced_at = stale.updated_at - timedelta(seconds=1)
    unplanned = await _plan(db, unplanned_day)
    await db.execute(
        delete(DailyExecutionDecision).where(
            DailyExecutionDecision.dataset_key == DATASET,
            DailyExecutionDecision.trade_date == unplanned_day,
        )
    )
    await db.execute(
        delete(MissingDeliveryAlert).where(
            MissingDeliveryAlert.dataset_key == DATASET,
            MissingDeliveryAlert.expected_data_date == unplanned_day,
        )
    )
    for table in (DailyDeliveryPart, DailyDeliveryPlanSummary, DailyDeliveryPlan):
        await db.execute(delete(table).where(table.plan_id == unplanned.plan_id))
    await db.commit()

    response = await client.get(
        URL,
        params={
            "date_from": (first - timedelta(days=3)).isoformat(),
            "date_to": (today + timedelta(days=7)).isoformat(),
            "dataset_key": DATASET,
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    (row,) = body["rows"]
    assert (row["dataset_key"], row["provider"], row["market"]) == (DATASET, "finlab", "TW")
    cells = {cell["trade_date"]: cell for cell in row["cells"]}

    assert cells[(first - timedelta(days=1)).isoformat()]["state"] == "not_started"
    assert cells[complete_day.isoformat()]["state"] == "complete"
    assert cells[complete_day.isoformat()]["counts"]["data"] == 2
    assert cells[pending_day.isoformat()]["state"] == "incomplete"
    late = next(
        day
        for day in _weekdays(first, through)
        if day not in {complete_day, pending_day, unsynced_day, unplanned_day}
    )
    assert cells[late.isoformat()]["state"] == "late"
    assert cells[late.isoformat()]["open_alert"] is True
    assert cells[late.isoformat()]["counts"] == {
        "expected": 2,
        "data": 1,
        "no_data": 0,
        "missing": 1,
        "blocked": 0,
        "unavailable": 0,
        "mapping_gap": 0,
        "unlisted_data": 0,
    }
    assert cells[unsynced_day.isoformat()]["counts_synced"] is False
    assert cells[unsynced_day.isoformat()]["state"] == "late"
    assert cells[unplanned_day.isoformat()]["state"] == "not_planned"
    weekend = next(
        day for day in (first + timedelta(days=n) for n in range(7)) if day.weekday() == 5
    )
    assert cells[weekend.isoformat()]["state"] == "closed"
    future = [day for day in _weekdays(through + timedelta(days=1), today + timedelta(days=7))]
    assert future and all(cells[day.isoformat()]["state"] == "not_due" for day in future)
    future_weekend = next(
        today + timedelta(days=n) for n in range(1, 8) if (today + timedelta(days=n)).weekday() == 6
    )
    assert cells[future_weekend.isoformat()]["state"] == "closed"


def _backfill(day_statuses, *, provider="finlab", completed_at=None):
    days = sorted(day_statuses)
    request = HistoricalBackfillRequest(
        request_id=uuid7(),
        request_key=f"matrix-{uuid7()}",
        provider=provider,
        dataset_key=DATASET,
        market="TW",
        start_date=days[0],
        end_date=days[-1],
        status="partial",
        created_by="test",
    )
    request.items = [
        HistoricalBackfillItem(
            item_id=uuid7(),
            trade_date=day,
            status=status,
            completed_at=completed_at or utc_now(),
        )
        for day, status in day_statuses.items()
    ]
    return request


@pytest.mark.asyncio
async def test_settled_backfills_mark_dates_without_a_daily_plan(
    client: AsyncClient, test_session, admin_headers: dict
):
    db = test_session
    await _environment(db, ["finlab"], days=14)
    now = utc_now()
    today = now.date()
    first = today - timedelta(days=14)
    through = latest_due_date(DATASET, now)
    earlier = list(_weekdays(first - timedelta(days=21), first - timedelta(days=1)))
    done, partial, retried, failed, untouched = earlier[-5:]
    planned_day, unplanned_day = list(_weekdays(first, through - timedelta(days=1)))[:2]
    unplanned = await _plan(db, unplanned_day)
    await db.execute(
        delete(DailyExecutionDecision).where(
            DailyExecutionDecision.dataset_key == DATASET,
            DailyExecutionDecision.trade_date == unplanned_day,
        )
    )
    for table in (DailyDeliveryPart, DailyDeliveryPlanSummary, DailyDeliveryPlan):
        await db.execute(delete(table).where(table.plan_id == unplanned.plan_id))
    finished = now - timedelta(hours=3)
    db.add_all(
        [
            _backfill(
                {
                    done: "completed",
                    partial: "partial",
                    retried: "partial",
                    failed: "failed",
                    planned_day: "completed",
                    unplanned_day: "partial",
                },
                completed_at=finished,
            ),
            # A later request completed the date an earlier one left partial.
            _backfill({retried: "completed"}, completed_at=finished),
            # Another provider's backfill never marks this feed.
            _backfill({untouched: "completed"}, provider="twelve_data"),
        ]
    )
    await db.commit()

    response = await client.get(
        URL,
        params={
            "date_from": earlier[0].isoformat(),
            "date_to": today.isoformat(),
            "dataset_key": DATASET,
        },
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    (row,) = response.json()["rows"]
    cells = {cell["trade_date"]: cell for cell in row["cells"]}
    state = {day: cells[day.isoformat()]["state"] for day in earlier[-5:]}
    assert state == {
        done: "backfilled",
        partial: "backfill_partial",
        retried: "backfilled",
        failed: "not_started",
        untouched: "not_started",
    }
    assert datetime.fromisoformat(cells[done.isoformat()]["backfill_completed_at"]) == finished
    assert cells[failed.isoformat()]["backfill_completed_at"] is None
    assert cells[unplanned_day.isoformat()]["state"] == "backfill_partial"
    # A planned date keeps its plan's state; the backfill already reclassified its counts.
    planned = cells[planned_day.isoformat()]
    assert planned["plan_id"] is not None
    assert planned["state"] in {"complete", "incomplete", "late"}
    assert planned["backfill_completed_at"] is None


@pytest.mark.asyncio
async def test_range_is_bounded_and_defaults_to_four_weeks(
    client: AsyncClient, test_session, admin_headers: dict
):
    await _environment(test_session, ["finlab"], days=1)
    response = await client.get(URL, headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert (
        datetime.fromisoformat(body["date_to"]) - datetime.fromisoformat(body["date_from"])
    ).days == 27
    too_long = await client.get(
        URL, params={"date_from": "2026-01-01", "date_to": "2026-03-31"}, headers=admin_headers
    )
    reversed_range = await client.get(
        URL, params={"date_from": "2026-03-02", "date_to": "2026-03-01"}, headers=admin_headers
    )
    assert too_long.status_code == reversed_range.status_code == 422


@pytest.mark.asyncio
async def test_matrix_costs_the_same_for_one_feed_and_many_over_weeks(
    client: AsyncClient, test_session, admin_headers: dict
):
    await _environment(test_session, ["finlab"], days=1)
    with count_statements(test_session) as small:
        assert (await client.get(URL, headers=admin_headers)).status_code == 200
    await _environment(test_session, ["taifex", "twelve_data", "hkex"], days=40)
    with count_statements(test_session) as large:
        response = await client.get(
            URL,
            params={
                "date_from": (utc_now().date() - timedelta(days=61)).isoformat(),
            },
            headers=admin_headers,
        )
    assert response.status_code == 200
    assert len(response.json()["rows"]) > 5
    assert_within_budget(small, large, ceiling=14)


@pytest.mark.asyncio
async def test_plan_list_reads_stored_counts_in_one_statement_per_page(
    client: AsyncClient, test_session, admin_headers: dict
):
    url = "/api/v1/admin/delivery-plans"
    params = {
        "execution_profile": "full_market",
        "page": 1,
        "page_size": 100,
        "summary_only": "true",
    }
    await _environment(test_session, ["finlab"], days=1)
    with count_statements(test_session) as small:
        assert (await client.get(url, params=params, headers=admin_headers)).status_code == 200
    await _environment(test_session, ["taifex", "twelve_data"], days=30)
    with count_statements(test_session) as large:
        response = await client.get(url, params=params, headers=admin_headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) > 25
    assert all(item["parts"] == [] and item["counts_synced"] for item in data)
    assert_within_budget(small, large, ceiling=7)


@pytest.mark.asyncio
async def test_plan_list_marks_unsynced_counts_without_reclassifying(
    client: AsyncClient, test_session, admin_headers: dict
):
    await _environment(test_session, ["finlab"], days=3)
    plan = await test_session.scalar(
        select(DailyDeliveryPlan).where(DailyDeliveryPlan.dataset_key == DATASET).limit(1)
    )
    stored = await test_session.get(DailyDeliveryPlanSummary, plan.plan_id)
    stored.parts_synced_at = datetime.combine(plan.trade_date, time(), timezone.utc)
    await test_session.commit()
    response = await client.get(
        "/api/v1/admin/delivery-plans",
        params={
            "execution_profile": "full_market",
            "dataset_key": DATASET,
            "trade_date": plan.trade_date.isoformat(),
            "page": 1,
            "page_size": 25,
            "summary_only": "true",
        },
        headers=admin_headers,
    )
    (item,) = response.json()["data"]
    assert item["counts_synced"] is False
    # The stored counts are reported as they are instead of a fresh classification.
    assert (item["summary"]["data"], item["summary"]["missing"]) == (1, 1)
