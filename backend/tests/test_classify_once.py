"""A write classifies its plan once; historical lookups run only for backfilled dates (#725)."""

import pytest
from sqlalchemy import select

from app.models.registry import (
    DailyDeliveryMember,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
    MissingDeliveryAlert,
)
from app.services import full_market
from app.services.delivery_monitor import settle_run_summaries
from app.services.delivery_plan_summary import refresh_plan_summary
from app.services.ingestion import IngestionService
from app.services.normalize.contracts import FuturesEODContractNormalizer
from app.utils import utc_now
from tests.test_delivery_plan_summary import _count_reconciles, summary
from tests.test_full_market import DAY, _activated_plan, _dataset, _quote


async def _alert_status(db, alert_id):
    return await db.scalar(
        select(MissingDeliveryAlert.status)
        .where(MissingDeliveryAlert.alert_id == alert_id)
        .execution_options(populate_existing=True)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [True, False], ids=["worker", "inline"])
async def test_completing_write_classifies_its_part_once_and_resolves_the_alert(
    test_session, monkeypatch, deferred
):
    dataset, _, plan = await _activated_plan(test_session, count=1)
    part = plan.parts[0]
    members = list(
        await test_session.scalars(
            select(DailyDeliveryMember)
            .where(DailyDeliveryMember.plan_id == plan.plan_id)
            .order_by(DailyDeliveryMember.member_key)
        )
    )
    member, *others = members
    for other in others:
        other.outcome = "no_data"
    await refresh_plan_summary(test_session, plan.plan_id)
    now = utc_now()
    alert = MissingDeliveryAlert(
        dataset_key=dataset.dataset_key,
        source="taifex",
        schema_id="futures_eod",
        schema_version=1,
        expected_data_date=DAY,
        status="open",
        first_detected_at=now,
        last_detected_at=now,
    )
    test_session.add(alert)
    run = await IngestionService(test_session).create_ingestion_run(
        dataset.dataset_key,
        source="taifex",
        delivery_part_id=part.part_id,
        schema_id="futures_eod",
        schema_version=1,
        batch_data_date=DAY,
    )
    parts: list[int] = []
    calls, _ = _count_reconciles(monkeypatch, parts)
    config = {**dataset.config, "_defer_summary_refresh": True} if deferred else dataset.config
    normalizer = FuturesEODContractNormalizer(test_session, config)
    await normalizer.process({"data": [_quote(session=member.session)]}, run.run_id, commit=False)
    if deferred:
        # The worker settles the summary and the alert after its final fields.
        assert parts == []
        assert await _alert_status(test_session, alert.alert_id) == "open"
        await settle_run_summaries(test_session, run)
    assert parts == [len(part.member_keys)] and calls == []
    assert await _alert_status(test_session, alert.alert_id) == "resolved"
    stored = await summary(test_session, plan.plan_id)
    assert stored.data_count + stored.no_data_count == stored.expected_count


@pytest.mark.asyncio
async def test_settle_keeps_the_alert_open_while_other_members_are_missing(
    test_session, monkeypatch
):
    dataset, _, plan = await _activated_plan(test_session, count=1)
    member = await test_session.scalar(
        select(DailyDeliveryMember)
        .where(DailyDeliveryMember.plan_id == plan.plan_id)
        .order_by(DailyDeliveryMember.member_key)
    )
    now = utc_now()
    alert = MissingDeliveryAlert(
        dataset_key=dataset.dataset_key,
        source="taifex",
        schema_id="futures_eod",
        schema_version=1,
        expected_data_date=DAY,
        status="open",
        first_detected_at=now,
        last_detected_at=now,
    )
    test_session.add(alert)
    run = await IngestionService(test_session).create_ingestion_run(
        dataset.dataset_key,
        source="taifex",
        delivery_part_id=plan.parts[0].part_id,
        schema_id="futures_eod",
        schema_version=1,
        batch_data_date=DAY,
    )
    normalizer = FuturesEODContractNormalizer(test_session, dataset.config)
    await normalizer.process({"data": [_quote(session=member.session)]}, run.run_id, commit=False)
    stored = await summary(test_session, plan.plan_id)
    assert stored.data_count == 1 < stored.expected_count
    assert await _alert_status(test_session, alert.alert_id) == "open"


@pytest.mark.asyncio
async def test_create_plan_classifies_the_new_plan_once(test_session, monkeypatch):
    calls, _ = _count_reconciles(monkeypatch)
    _, _, plan = await _activated_plan(test_session, count=1)
    assert calls == [plan.plan_id]
    stored = await summary(test_session, plan.plan_id)
    assert (plan.summary.expected, plan.summary.missing) == (
        stored.expected_count,
        stored.missing_count,
    )
    assert plan.status == "incomplete"
    assert sorted(key for part in plan.parts for key in part.member_keys) == sorted(
        await test_session.scalars(
            select(DailyDeliveryMember.member_key).where(
                DailyDeliveryMember.plan_id == plan.plan_id
            )
        )
    )


@pytest.mark.asyncio
async def test_historical_lookups_run_only_for_a_backfilled_date(test_session, monkeypatch):
    dataset, body, plan_response = await _activated_plan(test_session, "tw_equity_eod", count=1)
    plan = await test_session.get(full_market.DailyDeliveryPlan, plan_response.plan_id)
    lookups: list[str] = []
    repairs, no_data = full_market._historical_repairs, full_market._historical_no_data

    async def counted_repairs(*args, **kwargs):
        lookups.append("repairs")
        return await repairs(*args, **kwargs)

    async def counted_no_data(*args, **kwargs):
        lookups.append("no_data")
        return await no_data(*args, **kwargs)

    monkeypatch.setattr(full_market, "_historical_repairs", counted_repairs)
    monkeypatch.setattr(full_market, "_historical_no_data", counted_no_data)
    before = await full_market.classify_plan(test_session, plan)
    assert lookups == []

    # Another feed's backfill of the date leaves this plan's lookups skipped.
    await _dataset(test_session, "tw_etf_eod")
    other = HistoricalBackfillRequest(
        request_key="other-feed",
        provider=body.provider,
        dataset_key="tw_etf_eod",
        market="TW",
        start_date=DAY,
        end_date=DAY,
        created_by="owner",
    )
    request = HistoricalBackfillRequest(
        request_key="this-feed",
        provider=body.provider,
        dataset_key=dataset.dataset_key,
        market="TW",
        start_date=DAY,
        end_date=DAY,
        created_by="owner",
    )
    test_session.add_all([other, request])
    await test_session.flush()
    test_session.add(HistoricalBackfillItem(request_id=other.request_id, trade_date=DAY))
    await test_session.flush()
    await full_market.classify_plan(test_session, plan)
    assert lookups == []

    test_session.add(HistoricalBackfillItem(request_id=request.request_id, trade_date=DAY))
    await test_session.flush()
    after = await full_market.classify_plan(test_session, plan)
    assert lookups == ["repairs", "no_data"]
    assert [state.state for state in after] == [state.state for state in before]
