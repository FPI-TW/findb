"""The Dashboard overview reads keep a fixed statement count as schedulers, feeds and days grow.

The overview polls market freshness, schedulers and queue health every 60
seconds. Each must cost the same number of statements for one provider with a
fresh start as for several providers with weeks of plans, decisions and alerts.
"""

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.canonical import CalendarMarket, CalendarRevisionDay, CalendarYearRevision
from app.models.registry import (
    DailyDeliveryPart,
    DailyDeliveryPlan,
    DailyDeliveryPlanSummary,
    DailyExecutionDecision,
    DatasetRegistry,
    FullMarketAdmission,
    FullMarketAdmissionFeed,
    FullMarketDatasetState,
    MissingDeliveryAlert,
    SchedulerControl,
    SchedulerDataset,
    UniverseRelease,
)
from app.services.effective_calendar import MARKETS
from app.services.provider_metadata import dataset_calendar_market
from app.services.source_clients import PROVIDER_DATASET_SCOPE
from app.utils import utc_now, uuid7
from scripts.seed_data import DATASETS
from tests.query_budget import assert_within_budget, count_statements

ENDPOINTS = {
    "/api/v1/admin/market-freshness?execution_profile=full_market": 25,
    "/api/v1/admin/schedulers?execution_profile=full_market": 15,
    "/api/v1/admin/queue/health": 10,
}
# Every statement the three overview reads issue together, auth included.
OVERVIEW_CEILING = 45


async def _calendar(db, market: str, year: int) -> None:
    existing = await db.scalar(
        select(CalendarYearRevision.id).where(
            CalendarYearRevision.market == market, CalendarYearRevision.year == year
        )
    )
    if existing is not None:
        return
    if await db.get(CalendarMarket, market) is None:
        db.add(
            CalendarMarket(
                market=market, display_name=market, timezone=MARKETS[market][0], weekend_days=[5, 6]
            )
        )
        await db.flush()
    start = date(year, 1, 1)
    count = (date(year, 12, 31) - start).days + 1
    revision = CalendarYearRevision(
        market=market,
        year=year,
        revision=1,
        status="published",
        expected_days=count,
        actual_days=count,
        timezone=MARKETS[market][0],
        source_kind="test",
    )
    db.add(revision)
    await db.flush()
    days = [start + timedelta(days=offset) for offset in range(count)]
    db.add_all(
        CalendarRevisionDay(
            calendar_revision_id=revision.id,
            trade_date=day,
            is_open=day.weekday() < 5,
            day_status="open" if day.weekday() < 5 else "closed",
            source_kind="test",
        )
        for day in days
    )


async def _provider(db, provider: str, *, days: int) -> None:
    """A running control with every feed admitted, a baseline and `days` of history."""
    today = utc_now().date()
    first = today - timedelta(days=days)
    definitions = [
        item for item in DATASETS if item["dataset_key"] in PROVIDER_DATASET_SCOPE[provider]
    ]
    key = f"full_market_{provider}_v1"
    control = SchedulerControl(
        scheduler_key=key,
        execution_profile="full_market",
        provider=provider,
        slot_id="taiwan_market_window",
        scheduled_local_time=time(18),
        timezone="Asia/Taipei",
        desired_state="running",
        observed_state="running",
        revision=2,
    )
    db.add(control)
    datasets = []
    for definition in definitions:
        dataset = await db.get(DatasetRegistry, definition["dataset_key"])
        if dataset is None:
            dataset = DatasetRegistry(**deepcopy(definition))
            db.add(dataset)
        dataset.is_active = True
        datasets.append(dataset)
    await db.flush()
    admission = FullMarketAdmission(
        admission_id=uuid7(), scheduler_key=key, control_revision=2, capacity={}
    )
    db.add(admission)
    await db.flush()
    for dataset in datasets:
        db.add(SchedulerDataset(scheduler_key=key, dataset_key=dataset.dataset_key))
        release = UniverseRelease(
            release_id=uuid7(),
            dataset_key=dataset.dataset_key,
            provider=provider,
            effective_date=first,
            observed_at=datetime.combine(first, time(), timezone.utc),
            source_timezone="UTC",
            evidence=[],
            member_sha256="a" * 64,
            semantic_member_sha256="a" * 64,
            member_count=2,
            change_count=0,
            change_ratio=0,
            status="published",
        )
        db.add(release)
        await db.flush()
        db.add(
            FullMarketAdmissionFeed(
                admission_id=admission.admission_id,
                dataset_key=dataset.dataset_key,
                baseline_id=release.release_id,
            )
        )
        db.add(
            FullMarketDatasetState(
                dataset_key=dataset.dataset_key,
                execution_profile="full_market",
                first_start_date=first,
            )
        )
        for offset in range(days):
            day = first + timedelta(days=offset)
            if day.weekday() >= 5:
                db.add(
                    DailyExecutionDecision(
                        dataset_key=dataset.dataset_key,
                        provider=provider,
                        trade_date=day,
                        execution_profile="full_market",
                        kind="skipped_calendar",
                        snapshot={},
                    )
                )
                continue
            plan = DailyDeliveryPlan(
                plan_id=uuid7(),
                dataset_key=dataset.dataset_key,
                provider=provider,
                trade_date=day,
                release_id=release.release_id,
                execution_profile="full_market",
                activation_cutoff=first,
                deadline_at=datetime.combine(day, time(23), timezone.utc),
                member_sha256="a" * 64,
                expected_count=2,
            )
            db.add(plan)
            await db.flush()
            db.add(
                DailyExecutionDecision(
                    dataset_key=dataset.dataset_key,
                    provider=provider,
                    trade_date=day,
                    execution_profile="full_market",
                    kind="planned",
                    plan_id=plan.plan_id,
                    snapshot={},
                )
            )
            db.add(
                DailyDeliveryPart(
                    plan_id=plan.plan_id,
                    part_number=1,
                    work_item_id=f"{dataset.dataset_key}:{day}",
                    member_sha256="a" * 64,
                    expected_count=2,
                    data_count=1,
                    no_data_count=0,
                    missing_count=1,
                    blocked_count=0,
                    unavailable_count=0,
                    mapping_gap_count=0,
                )
            )
            stamp = utc_now()
            db.add(
                DailyDeliveryPlanSummary(
                    plan_id=plan.plan_id,
                    expected_count=2,
                    data_count=1,
                    no_data_count=0,
                    missing_count=1,
                    blocked_count=0,
                    updated_at=stamp,
                    parts_synced_at=stamp,
                )
            )
            db.add(
                MissingDeliveryAlert(
                    dataset_key=dataset.dataset_key,
                    source=provider,
                    execution_profile="full_market",
                    expected_data_date=day,
                    schema_id=str(dataset.config.get("schema_id", "market_eod")),
                    schema_version=int(dataset.config.get("current_schema_version", 1)),
                    status="open",
                    details={},
                )
            )
    await db.flush()


async def _environment(db, providers: list[str], *, days: int) -> None:
    today = utc_now().date()
    years = {today.year, (today - timedelta(days=days)).year}
    markets = {
        dataset_calendar_market(item["dataset_key"], item["market"])
        for provider in providers
        for item in DATASETS
        if item["dataset_key"] in PROVIDER_DATASET_SCOPE[provider]
    }
    for market in sorted(markets):
        for year in sorted(years):
            await _calendar(db, market, year)
    for provider in providers:
        await _provider(db, provider, days=days)
    await db.commit()


async def _counts(client: AsyncClient, db, headers: dict) -> dict:
    counts = {}
    for url in ENDPOINTS:
        with count_statements(db) as statements:
            response = await client.get(url, headers=headers)
        assert response.status_code == 200, response.text
        counts[url] = statements
    return counts


@pytest.mark.asyncio
async def test_overview_reads_cost_the_same_for_one_and_many_schedulers(
    client: AsyncClient, test_session, admin_headers: dict
):
    await _environment(test_session, ["finlab"], days=1)
    small = await _counts(client, test_session, admin_headers)
    await _environment(test_session, ["taifex", "twelve_data", "hkex"], days=30)
    large = await _counts(client, test_session, admin_headers)

    report = {url: (len(small[url]), len(large[url])) for url in ENDPOINTS}
    for url, ceiling in ENDPOINTS.items():
        try:
            assert_within_budget(small[url], large[url], ceiling=ceiling)
        except AssertionError as error:
            raise AssertionError(f"{url}: {report}\n{error}") from None
    assert sum(len(item) for item in large.values()) <= OVERVIEW_CEILING
