"""Read-only market freshness projection and endpoint behavior."""

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import get_settings
from app.models.canonical import CalendarMarket, CalendarRevisionDay, CalendarYearRevision
from app.models.raw import RawMarketPayload
from app.models.registry import (
    DatasetRegistry,
    IngestionRun,
    MissingDeliveryAlert,
    SchedulerControl,
    SchedulerDataset,
)
from app.schemas.serve import MarketFreshnessSummaryResponse
from app.services.market_freshness import list_market_freshness
from app.utils import uuid7
from scripts.seed_data import DATASETS

NOW = datetime(2026, 7, 22, 8, tzinfo=timezone.utc)

FULL_MARKET_SCOPES = {
    "finlab": ["tw_equity_eod", "tw_etf_eod"],
    "shioaji": ["tw_equity_minute", "tw_etf_minute"],
    "taifex": ["tw_futures_eod"],
    "twelve_data": ["us_equity_eod", "hk_equity_eod"],
}


async def _setup_full_market(session, provider: str):
    datasets = {}
    for definition in DATASETS:
        if definition["dataset_key"] in FULL_MARKET_SCOPES[provider]:
            dataset = DatasetRegistry(**deepcopy(definition))
            session.add(dataset)
            datasets[dataset.dataset_key] = dataset
    control = SchedulerControl(
        scheduler_key=f"full_market_{provider}_v1",
        provider=provider,
        slot_id="taiwan_market_window",
        scheduled_local_time=time(18),
        timezone="Asia/Taipei",
        desired_state="stopped",
        observed_state="stopped",
        revision=1,
    )
    session.add(control)
    await session.flush()
    session.add_all(
        [SchedulerDataset(scheduler_key=control.scheduler_key, dataset_key=key) for key in datasets]
    )
    await _seed_published_calendar_year(session, market="TW", year=2026)
    await _seed_published_calendar_year(session, market="US", year=2026)
    await session.flush()
    return control, datasets


def _enable_full_market(dataset):
    config = deepcopy(dataset.config)
    config["full_market"].update(
        enabled=True, readiness_approved=True, mode="acceptance", activation_date="2026-07-22"
    )
    dataset.config = config


def _config(sources: list[str]) -> dict:
    return {
        "schema_id": "market_eod",
        "current_schema_version": 1,
        "delivery_expectation": {
            "latest_date": {
                "calendar_market": "TW",
                "timezone": "Asia/Taipei",
                "market_close_time": "13:30:00",
                "availability_grace_minutes": 60,
                "action": "warn",
            },
            "schedule": {
                "enabled": True,
                "slot_id": "taiwan_market_window",
                "local_time": "14:30:00",
                "timezone": "Asia/Taipei",
                "expected_sources": sources,
            },
        },
    }


async def _setup(
    session,
    sources: list[str] = ["finlab"],
    closed_calendar_days: set[date] | None = None,
) -> None:
    session.add(
        DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="TW",
            asset_class="equity",
            market="TW",
            is_active=True,
            config=_config(sources),
        )
    )
    # Freshness cards are sourced exclusively from the durable scheduler
    # control plane.  A dataset delivery schedule alone is not a definition.
    for source in sources[:1]:
        scheduler_key = f"{source}_tw_equity_eod_v1"
        session.add(
            SchedulerControl(
                scheduler_key=scheduler_key,
                provider=source,
                slot_id="taiwan_market_window",
                scheduled_local_time=time(14, 30),
                timezone="Asia/Taipei",
                desired_state="running",
                observed_state="running",
                revision=1,
            )
        )
        await session.flush()
        session.add(SchedulerDataset(scheduler_key=scheduler_key, dataset_key="tw_equity_eod"))
    await _seed_published_calendar_year(
        session,
        market="TW",
        year=2026,
        closed_calendar_days=closed_calendar_days,
    )
    await session.commit()


async def _seed_published_calendar_year(
    session,
    *,
    market: str,
    year: int,
    closed_calendar_days: set[date] | None = None,
) -> None:
    if await session.get(CalendarMarket, market) is None:
        calendar_timezone = "America/New_York" if market == "US" else "Asia/Taipei"
        session.add(
            CalendarMarket(
                market=market,
                display_name=market,
                timezone=calendar_timezone,
                weekend_days=[5, 6],
            )
        )
        await session.flush()
    start = date(year, 1, 1)
    count = (date(year, 12, 31) - start).days + 1
    closed_calendar_days = closed_calendar_days or set()
    revision = CalendarYearRevision(
        market=market,
        year=year,
        revision=1,
        status="published",
        expected_days=count,
        actual_days=count,
        timezone="America/New_York" if market == "US" else "Asia/Taipei",
        source_kind="test",
    )
    session.add(revision)
    await session.flush()
    session.add_all(
        [
            CalendarRevisionDay(
                calendar_revision_id=revision.id,
                trade_date=start + timedelta(days=offset),
                is_open=(start + timedelta(days=offset)).weekday() < 5
                and start + timedelta(days=offset) not in closed_calendar_days,
                day_status=(
                    "open"
                    if (start + timedelta(days=offset)).weekday() < 5
                    and start + timedelta(days=offset) not in closed_calendar_days
                    else "closed"
                ),
                source_kind="test",
            )
            for offset in range(count)
        ]
    )


def _run(
    source: str,
    data_date: date,
    status: str = "completed",
    *,
    created_at: datetime = NOW,
    failure_code: str = "NORMALIZATION_FAILED",
) -> IngestionRun:
    return IngestionRun(
        run_id=uuid7(),
        dataset_key="tw_equity_eod",
        source=source,
        schema_id="market_eod",
        schema_version=1,
        batch_data_date=data_date,
        delivery_mode="full_snapshot",
        is_rerun=False,
        status=status,
        completed_at=created_at if status == "completed" else None,
        created_at=created_at,
        failure_code=failure_code if status == "failed" else None,
        total_records=2,
        success_records=2 if status == "completed" else 0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", FULL_MARKET_SCOPES)
async def test_dormant_full_market_reports_prerequisites_without_runtime_failure(
    test_session, provider
):
    await _setup_full_market(test_session, provider)
    row = (await list_market_freshness(test_session, now=NOW))[0]
    assert row.monitor_kind == "full_market"
    assert row.activation_state == "not_activated"
    assert row.configuration_status == "ready"
    assert row.configuration_errors == ()
    assert row.active_dataset_keys == ()
    assert row.status == "not_due"
    assert row.feed_count == row.fresh_feed_count == row.late_feed_count == 0
    assert len(row.pending_feeds) == len(FULL_MARKET_SCOPES[provider])
    assert all("activation_disabled" in feed.blockers for feed in row.pending_feeds)
    assert all(not feed.runtime_eligible for feed in row.feeds)
    if provider in {"taifex", "twelve_data"}:
        unavailable = next(
            feed
            for feed in row.pending_feeds
            if feed.dataset_key in {"tw_futures_eod", "hk_equity_eod"}
        )
        assert "dataset_inactive" in unavailable.blockers
        assert "calendar_does_not_cover_evaluation_date" in unavailable.blockers


@pytest.mark.asyncio
async def test_partial_full_market_aggregates_enabled_scope_and_keeps_bounded_details(test_session):
    control, datasets = await _setup_full_market(test_session, "finlab")
    _enable_full_market(datasets["tw_equity_eod"])
    control.desired_state = "running"
    test_session.add(_run("finlab", date(2026, 7, 22)))
    await test_session.flush()
    row = (await list_market_freshness(test_session, now=NOW))[0]
    assert row.configuration_status == "ready"
    assert row.activation_state == "activated"
    assert row.active_dataset_keys == ("tw_equity_eod",)
    assert row.status == "fresh"
    assert row.feed_count == row.fresh_feed_count == 1
    assert row.pending_feeds[0].dataset_key == "tw_etf_eod"
    assert row.last_successful_update_at == NOW
    assert row.last_heartbeat_at is None
    assert row.heartbeat_age_seconds is None


@pytest.mark.asyncio
async def test_running_full_market_without_enabled_scope_is_configuration_error(test_session):
    control, _ = await _setup_full_market(test_session, "shioaji")
    control.desired_state = "running"
    await test_session.flush()
    row = (await list_market_freshness(test_session, now=NOW))[0]
    assert "full_market_no_enabled_datasets" in row.configuration_errors


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("market", "CN"), ("asset_class", "futures")])
@pytest.mark.parametrize("enabled", [False, True])
async def test_full_market_registry_contract_scope_conflicts_remain_configuration_errors(
    test_session, client, admin_headers, field, value, enabled
):
    _, datasets = await _setup_full_market(test_session, "finlab")
    dataset = datasets["tw_equity_eod"]
    if enabled:
        _enable_full_market(dataset)
    setattr(dataset, field, value)
    await test_session.commit()

    row = (await list_market_freshness(test_session, now=NOW))[0]
    assert row.configuration_status == "error"
    assert "tw_equity_eod:contract_scope_mismatch" in row.configuration_errors
    assert row.activation_state == ("activated" if enabled else "not_activated")
    pending_etf = next(feed for feed in row.pending_feeds if feed.dataset_key == "tw_etf_eod")
    assert pending_etf.blockers == (
        "activation_disabled",
        "dataset_inactive",
        "readiness_not_approved",
    )

    response = await client.get("/api/v1/admin/market-freshness", headers=admin_headers)
    assert response.status_code == 200
    payload = response.json()["data"][0]
    assert payload["configuration_status"] == "error"
    assert "tw_equity_eod:contract_scope_mismatch" in payload["configuration_errors"]
    assert any(feed["dataset_key"] == "tw_etf_eod" for feed in payload["pending_feeds"])


@pytest.mark.asyncio
async def test_enabled_full_market_inactive_and_missing_calendar_are_errors_even_stopped(
    test_session,
):
    _, datasets = await _setup_full_market(test_session, "taifex")
    _enable_full_market(datasets["tw_futures_eod"])
    await test_session.flush()
    row = (await list_market_freshness(test_session, now=NOW))[0]
    assert row.activation_state == "activated"
    assert "tw_futures_eod:dataset_inactive" in row.configuration_errors
    assert "tw_futures_eod:calendar_does_not_cover_evaluation_date" in row.configuration_errors
    assert row.configuration_status == "error"
    assert row.pending_feeds == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["governance", "provider", "schema", "version", "timezone", "mapping"]
)
async def test_dormant_full_market_structural_errors_are_not_hidden(test_session, invalid):
    control, datasets = await _setup_full_market(test_session, "finlab")
    config = deepcopy(datasets["tw_equity_eod"].config)
    if invalid == "governance":
        config["full_market"]["enabled"] = "false"
    elif invalid == "provider":
        control.provider = "taifex"
    elif invalid == "schema":
        config["schema_id"] = "unsupported_schema"
    elif invalid == "version":
        config["current_schema_version"] = -1
    elif invalid == "timezone":
        control.timezone = "Invalid/Timezone"
    else:
        from sqlalchemy import delete

        await test_session.execute(
            delete(SchedulerDataset).where(SchedulerDataset.scheduler_key == control.scheduler_key)
        )
    datasets["tw_equity_eod"].config = config
    await test_session.flush()
    row = (await list_market_freshness(test_session, now=NOW))[0]
    assert row.configuration_status == "error"
    assert row.configuration_errors


@pytest.mark.asyncio
async def test_deactivated_full_market_retains_original_activation_date(test_session):
    _, datasets = await _setup_full_market(test_session, "shioaji")
    config = deepcopy(datasets["tw_equity_minute"].config)
    config["full_market"].update(activation_date="2026-07-01", mode="off")
    datasets["tw_equity_minute"].config = config
    await test_session.flush()
    row = (await list_market_freshness(test_session, now=NOW))[0]
    assert row.activation_state == "deactivated"
    assert datasets["tw_equity_minute"].config["full_market"]["activation_date"] == "2026-07-01"


@pytest.mark.asyncio
async def test_full_market_endpoint_keeps_pending_metadata_without_feed_details(
    client, test_session, admin_headers
):
    await _setup_full_market(test_session, "taifex")
    await test_session.commit()
    response = await client.get(
        "/api/v1/admin/market-freshness?include_feeds=false", headers=admin_headers
    )
    assert response.status_code == 200
    row = response.json()["data"][0]
    assert row["monitor_kind"] == "full_market"
    assert row["activation_state"] == "not_activated"
    assert row["pending_feeds"][0]["dataset_key"] == "tw_futures_eod"
    assert "calendar_does_not_cover_evaluation_date" in row["pending_feeds"][0]["blockers"]
    assert row["feeds"] == []


@pytest.mark.asyncio
async def test_configured_market_without_runs_is_never_received(test_session):
    await _setup(test_session)
    rows = await list_market_freshness(test_session, now=NOW)
    assert len(rows) == 1
    assert rows[0].status == "never_received"
    assert rows[0].feeds[0].status == "never_received"
    assert rows[0].expected_data_date == date(2026, 7, 22)
    assert rows[0].monitor_kind == "bounded"
    assert rows[0].activation_state is None
    assert rows[0].feeds[0].runtime_eligible

    before_due = await list_market_freshness(
        test_session, now=datetime(2026, 7, 22, 4, tzinfo=timezone.utc)
    )
    assert before_due[0].status == "never_received"
    assert before_due[0].feeds[0].status == "never_received"


@pytest.mark.asyncio
async def test_twelve_data_freshness_turns_late_at_0915_taipei(test_session):
    config = {
        "schema_id": "market_eod",
        "current_schema_version": 1,
        "delivery_expectation": {
            "delivery_mode": "incremental",
            "latest_date": {
                "calendar_market": "US",
                "timezone": "America/New_York",
                "market_close_time": "16:00:00",
                "availability_grace_minutes": 120,
                "action": "warn",
            },
            "missing_delivery": {
                "action": "warn",
                "expected_sources": ["twelve_data"],
                "deadline_local_time": "09:15:00",
            },
            "schedule": {
                "enabled": True,
                "slot_id": "western_markets_window",
                "local_time": "08:15:00",
                "timezone": "Asia/Taipei",
                "target_date_lag_days": 1,
                "expected_sources": ["twelve_data"],
            },
        },
    }
    test_session.add(
        DatasetRegistry(
            dataset_key="us_equity_eod",
            name="US",
            asset_class="equity",
            market="US",
            is_active=True,
            config=config,
        )
    )
    test_session.add(
        SchedulerControl(
            scheduler_key="twelve_data_us_common_stocks_daily_v1",
            provider="twelve_data",
            slot_id="western_markets_window",
            scheduled_local_time=time(8, 15),
            timezone="Asia/Taipei",
            desired_state="running",
            observed_state="running",
            revision=1,
        )
    )
    await test_session.flush()
    test_session.add(
        SchedulerDataset(
            scheduler_key="twelve_data_us_common_stocks_daily_v1",
            dataset_key="us_equity_eod",
        )
    )
    await _seed_published_calendar_year(test_session, market="US", year=2026)
    test_session.add(
        IngestionRun(
            run_id=uuid7(),
            dataset_key="us_equity_eod",
            source="twelve_data",
            schema_id="market_eod",
            schema_version=1,
            batch_data_date=date(2026, 7, 21),
            delivery_mode="incremental",
            is_rerun=False,
            status="completed",
            completed_at=datetime(2026, 7, 22, 1, 10, tzinfo=timezone.utc),
        )
    )
    await test_session.commit()

    before = (
        await list_market_freshness(
            test_session,
            market="US",
            now=datetime(2026, 7, 23, 1, 14, tzinfo=timezone.utc),
        )
    )[0]
    assert before.expected_data_date == date(2026, 7, 21)
    assert before.status == "fresh"

    due = (
        await list_market_freshness(
            test_session,
            market="US",
            now=datetime(2026, 7, 23, 1, 15, tzinfo=timezone.utc),
        )
    )[0]
    assert due.expected_data_date == date(2026, 7, 22)
    assert due.status == "late"
    assert due.feeds[0].latest_successful_data_date == date(2026, 7, 21)


@pytest.mark.asyncio
async def test_freshness_without_scheduler_control_is_empty(test_session):
    assert await list_market_freshness(test_session, now=NOW) == []


@pytest.mark.asyncio
async def test_registered_stopped_scheduler_and_invalid_dataset_stay_visible(test_session):
    """Scheduler definitions remain visible even when feed config is unusable."""
    session = test_session
    session.add(
        DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="TW",
            asset_class="equity",
            market="TW",
            is_active=True,
            config={"schema_id": "market_eod"},
        )
    )
    session.add(
        SchedulerControl(
            scheduler_key="finlab_tw_equity_eod_v1",
            provider="finlab",
            slot_id="taiwan_market_window",
            scheduled_local_time=time(14, 30),
            timezone="Asia/Taipei",
            desired_state="stopped",
            observed_state="stopped",
            revision=4,
        )
    )
    await session.flush()
    session.add(
        SchedulerDataset(scheduler_key="finlab_tw_equity_eod_v1", dataset_key="tw_equity_eod")
    )
    await _seed_published_calendar_year(session, market="TW", year=2026)
    await session.commit()

    rows = await list_market_freshness(session, now=NOW)
    assert len(rows) == 1
    row = rows[0]
    assert row.scheduler_key == "finlab_tw_equity_eod_v1"
    assert row.desired_state == "stopped"
    assert row.observed_state == "stopped"
    assert row.configuration_status == "error"
    assert row.feeds[0].configuration_error is not None
    assert row.feeds[0].expected_data_date is None


@pytest.mark.asyncio
async def test_freshness_uses_scheduler_control_provider_and_feed(test_session):
    await _setup(test_session, ["finlab", "bloomberg"])
    test_session.add(_run("finlab", date(2026, 7, 22)))
    await test_session.commit()
    row = (await list_market_freshness(test_session, now=NOW))[0]
    assert row.provider == "finlab"
    assert row.status == "fresh"
    assert row.coverage_data_date == date(2026, 7, 22)
    assert row.feeds[0].source == "finlab"


@pytest.mark.asyncio
async def test_freshness_clears_a_failure_recovered_by_a_newer_success(test_session):
    await _setup(test_session)
    test_session.add_all(
        [
            _run(
                "finlab",
                date(2026, 7, 21),
                "failed",
                created_at=datetime(2026, 7, 21, 7, tzinfo=timezone.utc),
                failure_code="RETRY_EXHAUSTED",
            ),
            _run(
                "finlab",
                date(2026, 7, 22),
                created_at=datetime(2026, 7, 22, 7, tzinfo=timezone.utc),
            ),
        ]
    )
    await test_session.commit()

    feed = (await list_market_freshness(test_session, now=NOW))[0].feeds[0]

    assert feed.status == "fresh"
    assert feed.last_failure_code is None


@pytest.mark.asyncio
async def test_freshness_keeps_a_failure_newer_than_the_latest_success(test_session):
    await _setup(test_session)
    test_session.add_all(
        [
            _run(
                "finlab",
                date(2026, 7, 21),
                created_at=datetime(2026, 7, 21, 7, tzinfo=timezone.utc),
            ),
            _run(
                "finlab",
                date(2026, 7, 22),
                "failed",
                created_at=datetime(2026, 7, 22, 7, tzinfo=timezone.utc),
                failure_code="RETRY_EXHAUSTED",
            ),
        ]
    )
    await test_session.commit()

    feed = (await list_market_freshness(test_session, now=NOW))[0].feeds[0]

    assert feed.status == "failed"
    assert feed.last_failure_code == "RETRY_EXHAUSTED"


@pytest.mark.asyncio
async def test_full_snapshot_freshness_keeps_expected_date_failure_despite_older_success(
    test_session,
):
    await _setup(test_session)
    test_session.add_all(
        [
            _run(
                "finlab",
                date(2026, 7, 22),
                "failed",
                created_at=datetime(2026, 7, 22, 7, tzinfo=timezone.utc),
                failure_code="RETRY_EXHAUSTED",
            ),
            _run(
                "finlab",
                date(2026, 7, 21),
                created_at=datetime(2026, 7, 22, 8, tzinfo=timezone.utc),
            ),
        ]
    )
    await test_session.commit()

    feed = (await list_market_freshness(test_session, now=NOW))[0].feeds[0]

    assert feed.last_failure_code == "RETRY_EXHAUSTED"


@pytest.mark.asyncio
async def test_incremental_freshness_requires_success_to_cover_the_expected_date(test_session):
    await _setup(test_session)
    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    dataset.config["delivery_expectation"]["delivery_mode"] = "incremental"
    test_session.add_all(
        [
            IngestionRun(
                run_id=uuid7(),
                dataset_key="tw_equity_eod",
                source="finlab",
                schema_id="market_eod",
                schema_version=1,
                batch_data_date=date(2026, 7, 22),
                delivery_mode="incremental",
                is_rerun=False,
                status="failed",
                failure_code="RETRY_EXHAUSTED",
                created_at=datetime(2026, 7, 22, 7, tzinfo=timezone.utc),
            ),
            IngestionRun(
                run_id=uuid7(),
                dataset_key="tw_equity_eod",
                source="finlab",
                schema_id="market_eod",
                schema_version=1,
                batch_data_date=date(2026, 7, 21),
                delivery_mode="backfill",
                coverage_start_date=date(2026, 7, 21),
                coverage_end_date=date(2026, 7, 21),
                is_rerun=False,
                status="completed",
                completed_at=datetime(2026, 7, 22, 8, tzinfo=timezone.utc),
                created_at=datetime(2026, 7, 22, 8, tzinfo=timezone.utc),
            ),
        ]
    )
    await test_session.commit()

    feed = (await list_market_freshness(test_session, now=NOW))[0].feeds[0]

    assert feed.last_failure_code == "RETRY_EXHAUSTED"

    test_session.add(
        IngestionRun(
            run_id=uuid7(),
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            batch_data_date=date(2026, 7, 22),
            delivery_mode="backfill",
            coverage_start_date=date(2026, 7, 21),
            coverage_end_date=date(2026, 7, 22),
            is_rerun=False,
            status="completed",
            completed_at=datetime(2026, 7, 22, 9, tzinfo=timezone.utc),
            created_at=datetime(2026, 7, 22, 9, tzinfo=timezone.utc),
        )
    )
    await test_session.commit()

    recovered_feed = (await list_market_freshness(test_session, now=NOW))[0].feeds[0]

    assert recovered_feed.last_failure_code is None


@pytest.mark.asyncio
async def test_incremental_freshness_counts_successful_backfill_and_reports_batch_date(
    test_session,
):
    await _setup(test_session)
    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    dataset.config["delivery_expectation"]["delivery_mode"] = "incremental"
    test_session.add(
        IngestionRun(
            run_id=uuid7(),
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            batch_data_date=date(2026, 7, 22),
            delivery_mode="backfill",
            coverage_start_date=date(2026, 7, 21),
            coverage_end_date=date(2026, 7, 22),
            is_rerun=False,
            status="completed",
            completed_at=NOW,
        )
    )
    await test_session.commit()

    row = (await list_market_freshness(test_session, now=NOW))[0]

    assert row.status == "fresh"
    assert row.feeds[0].status == "fresh"
    assert row.feeds[0].latest_successful_data_date == date(2026, 7, 22)
    assert row.coverage_data_date == date(2026, 7, 22)


@pytest.mark.asyncio
async def test_incremental_freshness_keeps_out_of_range_backfill_visible_but_late(
    test_session,
):
    await _setup(test_session)
    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    dataset.config["delivery_expectation"]["delivery_mode"] = "incremental"
    test_session.add(
        IngestionRun(
            run_id=uuid7(),
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            batch_data_date=date(2026, 7, 23),
            delivery_mode="backfill",
            coverage_start_date=date(2026, 7, 23),
            coverage_end_date=date(2026, 7, 23),
            is_rerun=False,
            status="completed",
            completed_at=NOW,
        )
    )
    await test_session.commit()

    row = (await list_market_freshness(test_session, now=NOW))[0]

    assert row.status == "late"
    assert row.feeds[0].status == "late"
    assert row.feeds[0].latest_successful_data_date == date(2026, 7, 23)
    assert row.coverage_data_date == date(2026, 7, 23)


@pytest.mark.asyncio
async def test_freshness_keeps_raw_fetched_at_distinct_from_normalization_completion(
    test_session,
):
    await _setup(test_session)
    run = _run("finlab", date(2026, 7, 22))
    test_session.add(run)
    await test_session.flush()
    raw = RawMarketPayload(
        raw_payload_id=uuid7(),
        source_client_id=None,
        dataset_key="tw_equity_eod",
        source="finlab",
        request_key="freshness-raw",
        idempotency_key="freshness-raw-idem",
        schema_id="market_eod",
        schema_version=1,
        payload={"data": []},
        fetched_at=datetime(2026, 7, 22, 7, tzinfo=timezone.utc),
        expire_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        run_id=run.run_id,
    )
    test_session.add(raw)
    run.raw_payload_id = raw.raw_payload_id
    await test_session.commit()

    row = (await list_market_freshness(test_session, now=NOW))[0]
    assert row.last_fetched_at == raw.fetched_at
    assert row.last_successful_update_at == NOW
    assert row.feeds[0].last_fetched_at == raw.fetched_at
    assert row.feeds[0].last_completed_at == NOW


@pytest.mark.asyncio
async def test_not_due_and_open_alert_are_read_only(test_session):
    await _setup(
        test_session,
        closed_calendar_days={
            date(2026, 7, 22) - timedelta(days=offset) for offset in range(1, 32)
        },
    )
    test_session.add_all(
        [
            _run("finlab", date(2026, 7, 21)),
            MissingDeliveryAlert(
                dataset_key="tw_equity_eod",
                source="finlab",
                schema_id="market_eod",
                schema_version=1,
                expected_data_date=date(2026, 7, 22),
                status="open",
                first_detected_at=NOW,
                last_detected_at=NOW,
            ),
        ]
    )
    await test_session.commit()
    rows = await list_market_freshness(
        test_session, now=datetime(2026, 7, 22, 4, tzinfo=timezone.utc)
    )
    assert rows[0].status == "not_due"
    assert rows[0].feeds[0].open_missing_delivery_alert is False
    alert = (await test_session.execute(select(MissingDeliveryAlert))).scalar_one()
    assert alert.status == "open"


@pytest.mark.asyncio
async def test_admin_filters_and_serve_redacts_feed_internals(
    client: AsyncClient, test_session, admin_headers
):
    await _setup(test_session)
    unauthorized = await client.get("/api/v1/admin/market-freshness")
    assert unauthorized.status_code == 401
    admin = await client.get(
        "/api/v1/admin/market-freshness?market=TW&slot_id=taiwan_market_window&status=never_received&include_feeds=false",
        headers=admin_headers,
    )
    assert admin.status_code == 200
    assert admin.json()["data"][0]["feeds"] == []
    for legacy_slot in ("us_0600", "global_0815", "tw_1430", "asia_1630"):
        rejected = await client.get(
            f"/api/v1/admin/market-freshness?slot_id={legacy_slot}",
            headers=admin_headers,
        )
        assert rejected.status_code == 422

    settings = get_settings()
    original = settings.SERVE_REQUIRE_AUTH
    settings.SERVE_REQUIRE_AUTH = True
    try:
        assert (await client.get("/api/v1/serve/market-freshness")).status_code == 401
    finally:
        settings.SERVE_REQUIRE_AUTH = original
    served = await client.get("/api/v1/serve/market-freshness?market=TW")
    assert served.status_code == 200
    assert "feeds" not in served.json()["data"][0]
    assert "dataset_key" not in str(served.json())


def test_serve_freshness_schema_has_a_closed_status_vocabulary():
    status_schema = MarketFreshnessSummaryResponse.model_json_schema()["properties"]["status"]
    assert status_schema["enum"] == [
        "not_due",
        "fresh",
        "partial",
        "late",
        "failed",
        "never_received",
    ]
