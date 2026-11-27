"""Admin detail reads load on demand: paged gaps, slim backfill lists, paged backfill items."""

from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.registry import DailyDeliveryMember, HistoricalBackfillItem
from app.services.historical_backfill import create_request
from tests.query_budget import assert_within_budget, count_statements
from tests.test_full_market import _activated_plan
from tests.test_full_market_lateness import _block
from tests.test_historical_backfill import _seed_scope

BACKFILLS = "/api/v1/admin/historical-backfills"
backfill_cohort = pytest.mark.usefixtures("full_release_cohort")
TODAY = date(2026, 9, 1)


async def _requests(db, count: int, *, start: int = 0, days: int = 3) -> list:
    created = []
    for index in range(start, start + count):
        request, _ = await create_request(
            db,
            provider="finlab",
            dataset_key="tw_equity_eod",
            start_date=TODAY - timedelta(days=days - 1),
            end_date=TODAY,
            request_key=f"detail-reads-{index}",
            created_by="operator",
            today=TODAY,
        )
        created.append(request)
    for item in await db.scalars(select(HistoricalBackfillItem)):
        # Evidence is the heavy part of an item; a list must never carry it.
        item.result_evidence = {"members": [{"key": f"m{n}", "raw": "x" * 200} for n in range(50)]}
    await db.commit()
    return created


@pytest.mark.asyncio
async def test_plan_gaps_filter_by_status_and_page(
    client: AsyncClient, test_session, admin_headers: dict
):
    _, body, plan = await _activated_plan(test_session, count=3)
    members = (
        await test_session.scalars(
            select(DailyDeliveryMember)
            .where(DailyDeliveryMember.plan_id == plan.plan_id)
            .order_by(DailyDeliveryMember.member_key)
        )
    ).all()
    await _block(test_session, plan, body.provider, members[0], "source_error")
    await _block(test_session, plan, body.provider, members[1], "source_error")
    await test_session.commit()
    url = f"/api/v1/admin/delivery-plans/{plan.plan_id}/summary"

    everything = (await client.get(url, headers=admin_headers)).json()
    assert everything["gaps_total"] == len(everything["gaps"]) == len(members)
    blocked = (
        await client.get(url, params={"gap_status": "blocked"}, headers=admin_headers)
    ).json()
    assert blocked["gaps_total"] == 2
    assert {gap["status"] for gap in blocked["gaps"]} == {"blocked"}
    # Counts stay the plan's whole counts whatever the gap filter.
    assert (blocked["blocked"], blocked["missing"]) == (
        everything["blocked"],
        everything["missing"],
    )
    first = (
        await client.get(
            url, params={"gap_status": "blocked", "gap_page_size": 1}, headers=admin_headers
        )
    ).json()
    second = (
        await client.get(
            url,
            params={"gap_status": "blocked", "gap_page_size": 1, "gap_page": 2},
            headers=admin_headers,
        )
    ).json()
    assert first["gaps_total"] == second["gaps_total"] == 2
    assert [gap["member_key"] for gap in first["gaps"] + second["gaps"]] == [
        gap["member_key"] for gap in blocked["gaps"]
    ]
    too_large = await client.get(url, params={"gap_page_size": 1001}, headers=admin_headers)
    assert too_large.status_code == 422


@backfill_cohort
@pytest.mark.asyncio
async def test_backfill_list_carries_counts_and_item_summaries_never_evidence(
    client: AsyncClient, test_session, admin_headers: dict
):
    await _seed_scope(test_session, TODAY)
    [request] = await _requests(test_session, 1)

    listed = (await client.get(BACKFILLS, headers=admin_headers)).json()["data"][0]
    assert listed["request_id"] == str(request.request_id)
    assert sum(listed["item_counts"].values()) == len(listed["items"]) > 0
    assert all(
        item["result_evidence"] is None and item["calendar_snapshot"] is None
        for item in listed["items"]
    )
    slim = (
        await client.get(BACKFILLS, params={"include_items": "false"}, headers=admin_headers)
    ).json()["data"][0]
    assert slim["items"] == [] and slim["item_counts"] == listed["item_counts"]

    items_url = f"{BACKFILLS}/{request.request_id}/items"
    page = (await client.get(items_url, params={"page_size": 1}, headers=admin_headers)).json()
    assert page["pagination"]["total_records"] == len(listed["items"])
    assert len(page["data"]) == 1 and page["data"][0]["result_evidence"] is None
    detailed = (
        await client.get(items_url, params={"include_evidence": "true"}, headers=admin_headers)
    ).json()["data"]
    assert [item["trade_date"] for item in detailed] == sorted(
        item["trade_date"] for item in listed["items"]
    )
    assert all(len(item["result_evidence"]["members"]) == 50 for item in detailed)
    assert all(item["calendar_snapshot"] is not None for item in detailed)
    missing = await client.get(
        f"{BACKFILLS}/00000000-0000-7000-8000-000000000000/items", headers=admin_headers
    )
    assert missing.status_code == 404


@backfill_cohort
@pytest.mark.asyncio
async def test_backfill_list_cost_and_size_do_not_grow_with_requests_or_evidence(
    client: AsyncClient, test_session, admin_headers: dict
):
    await _seed_scope(test_session, TODAY)
    await _requests(test_session, 1)
    params = {"page_size": 100, "include_items": "false"}
    with count_statements(test_session) as small:
        one = await client.get(BACKFILLS, params=params, headers=admin_headers)
    await _requests(test_session, 6, start=1, days=5)
    with count_statements(test_session) as large:
        many = await client.get(BACKFILLS, params=params, headers=admin_headers)
    assert one.status_code == many.status_code == 200
    assert len(many.json()["data"]) == 7
    assert_within_budget(small, large, ceiling=6)
    # Without items a request costs a fixed few hundred bytes, however large its evidence.
    assert len(many.content) < 7 * 1200
    with count_statements(test_session) as with_items:
        full = await client.get(BACKFILLS, params={"page_size": 100}, headers=admin_headers)
    assert len(with_items) == len(large) + 1
    assert "members" not in full.text
