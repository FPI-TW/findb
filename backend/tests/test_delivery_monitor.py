"""Missing-delivery monitor persistence and resolution behavior."""

import asyncio
from datetime import date, datetime, time, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.canonical import CalendarMarket, CalendarRevisionDay, CalendarYearRevision
from app.models.registry import DatasetRegistry, IngestionRun, MissingDeliveryAlert
from app.services.delivery_monitor import resolve_missing_delivery_for_run, scan_missing_deliveries
from app.services.delivery_policy import (
    DeliveryExpectation,
    LatestDatePolicy,
    resolve_expected_data_date,
)
from app.services.feed_scope import lock_feed_scope

NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)


def _config(*, action: str = "warn", sources: list[str] | None = None) -> dict:
    return {
        "schema_id": "market_eod",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "defaults": {"market": "TW", "asset_class": "equity", "currency": "TWD"},
        "delivery_expectation": {
            "latest_date": {
                "calendar_market": "TW",
                "timezone": "Asia/Taipei",
                "market_close_time": "13:30:00",
                "availability_grace_minutes": 60,
                "action": "warn",
            },
            "missing_delivery": {
                "action": action,
                "expected_sources": sources or [],
            },
        },
    }


def _twelve_data_config() -> dict:
    return {
        "schema_id": "market_eod",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "defaults": {"market": "US", "asset_class": "equity", "currency": "USD"},
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
                "deadline_local_time": "11:30:00",
            },
            "schedule": {
                "enabled": True,
                "slot_id": "western_markets_window",
                "local_time": "10:30:00",
                "timezone": "Asia/Taipei",
                "target_date_lag_days": 1,
                "expected_sources": ["twelve_data"],
            },
        },
    }


async def _seed_dataset(session, *, active: bool = True, config: dict | None = None) -> None:
    session.add(
        DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="TW equity",
            asset_class="equity",
            market="TW",
            is_active=active,
            config=config or _config(sources=["finlab"]),
        )
    )
    await _seed_published_calendar_year(session, market="TW", year=2026)
    await session.commit()


async def _seed_published_calendar_year(
    session,
    *,
    market: str,
    year: int,
    closed_dates: set[date] | None = None,
) -> None:
    if await session.get(CalendarMarket, market) is None:
        session.add(
            CalendarMarket(
                market=market,
                display_name=market,
                timezone="America/New_York" if market == "US" else "Asia/Taipei",
                weekend_days=[5, 6],
            )
        )
        await session.flush()
    start = date(year, 1, 1)
    count = (date(year, 12, 31) - start).days + 1
    closed_dates = closed_dates or set()
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
                and start + timedelta(days=offset) not in closed_dates,
                day_status=(
                    "open"
                    if (start + timedelta(days=offset)).weekday() < 5
                    and start + timedelta(days=offset) not in closed_dates
                    else "closed"
                ),
                source_kind="test",
            )
            for offset in range(count)
        ]
    )


def test_missing_delivery_config_is_opt_in_and_validated() -> None:
    assert DeliveryExpectation.model_validate({}).missing_delivery.action == "disabled"
    parsed = DeliveryExpectation.model_validate(
        {
            "missing_delivery": {
                "action": "warn",
                "expected_sources": ["finlab"],
                "deadline_local_time": "17:00:00",
            }
        }
    )
    assert parsed.missing_delivery.expected_sources == ["finlab"]
    assert parsed.missing_delivery.deadline_local_time == time(17)
    assert DeliveryExpectation.model_validate({}).missing_delivery.deadline_local_time is None
    with pytest.raises(ValidationError):
        DeliveryExpectation.model_validate(
            {"missing_delivery": {"action": "warn", "expected_sources": []}}
        )
    with pytest.raises(ValidationError):
        DeliveryExpectation.model_validate(
            {"missing_delivery": {"action": "reject", "expected_sources": ["finlab"]}}
        )
    for invalid_source in ("FinLab", "fin-lab", "fin.lab"):
        with pytest.raises(ValidationError):
            DeliveryExpectation.model_validate(
                {
                    "missing_delivery": {
                        "action": "warn",
                        "expected_sources": [invalid_source],
                    }
                }
            )
    with pytest.raises(ValidationError):
        DeliveryExpectation.model_validate(
            {
                "missing_delivery": {
                    "action": "warn",
                    "expected_sources": ["finlab"],
                    "deadline_local_time": "25:00:00",
                }
            }
        )


@pytest.mark.asyncio
async def test_public_calendar_resolver_honors_dst_and_grace(test_session) -> None:
    await _seed_published_calendar_year(test_session, market="US", year=2026)
    await test_session.commit()
    policy = LatestDatePolicy(
        calendar_market="US",
        timezone="America/New_York",
        market_close_time=time(16),
        availability_grace_minutes=60,
        action="warn",
    )

    before, before_reason = await resolve_expected_data_date(
        test_session,
        policy,
        datetime(2026, 3, 9, 20, 59, tzinfo=timezone.utc),
    )
    after, after_reason = await resolve_expected_data_date(
        test_session,
        policy,
        datetime(2026, 3, 9, 21, 0, tzinfo=timezone.utc),
    )
    assert before == date(2026, 3, 6)
    assert before_reason is None
    assert after == date(2026, 3, 9)
    assert after_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before_deadline", "at_deadline", "current_date", "prior_date"),
    [
        (
            datetime(2026, 7, 23, 3, 29, tzinfo=timezone.utc),
            datetime(2026, 7, 23, 3, 30, tzinfo=timezone.utc),
            date(2026, 7, 22),
            date(2026, 7, 21),
        ),
        (
            datetime(2026, 1, 15, 3, 29, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 3, 30, tzinfo=timezone.utc),
            date(2026, 1, 14),
            date(2026, 1, 13),
        ),
        (
            datetime(2026, 7, 25, 3, 29, tzinfo=timezone.utc),
            datetime(2026, 7, 25, 3, 30, tzinfo=timezone.utc),
            date(2026, 7, 24),
            date(2026, 7, 23),
        ),
    ],
    ids=("us-daylight-time", "us-standard-time", "weekend-deadline"),
)
async def test_twelve_data_operational_deadline_is_fixed_in_taipei(
    test_session,
    before_deadline: datetime,
    at_deadline: datetime,
    current_date: date,
    prior_date: date,
) -> None:
    await _seed_published_calendar_year(test_session, market="US", year=2026)
    await test_session.commit()
    policy = LatestDatePolicy(
        calendar_market="US",
        timezone="America/New_York",
        market_close_time=time(16),
        availability_grace_minutes=120,
        action="warn",
    )

    before = await resolve_expected_data_date(
        test_session,
        policy,
        before_deadline,
        strict_current_session=True,
        current_session_deadline=time(11, 30),
        operational_deadline_timezone="Asia/Taipei",
        target_date_lag_days=1,
    )
    due = await resolve_expected_data_date(
        test_session,
        policy,
        at_deadline,
        strict_current_session=True,
        current_session_deadline=time(11, 30),
        operational_deadline_timezone="Asia/Taipei",
        target_date_lag_days=1,
    )

    assert before == (prior_date, None)
    assert due == (current_date, None)


@pytest.mark.asyncio
async def test_twelve_data_deadline_keeps_last_session_across_us_holiday(test_session) -> None:
    await _seed_published_calendar_year(
        test_session,
        market="US",
        year=2026,
        closed_dates={date(2026, 7, 3)},
    )
    await test_session.commit()
    policy = LatestDatePolicy(
        calendar_market="US",
        timezone="America/New_York",
        market_close_time=time(16),
        availability_grace_minutes=120,
        action="warn",
    )

    before_holiday_deadline = await resolve_expected_data_date(
        test_session,
        policy,
        datetime(2026, 7, 3, 3, 29, tzinfo=timezone.utc),
        strict_current_session=True,
        current_session_deadline=time(11, 30),
        operational_deadline_timezone="Asia/Taipei",
        target_date_lag_days=1,
    )
    at_holiday_deadline = await resolve_expected_data_date(
        test_session,
        policy,
        datetime(2026, 7, 3, 3, 30, tzinfo=timezone.utc),
        strict_current_session=True,
        current_session_deadline=time(11, 30),
        operational_deadline_timezone="Asia/Taipei",
        target_date_lag_days=1,
    )
    after_closed_session = await resolve_expected_data_date(
        test_session,
        policy,
        datetime(2026, 7, 4, 3, 30, tzinfo=timezone.utc),
        strict_current_session=True,
        current_session_deadline=time(11, 30),
        operational_deadline_timezone="Asia/Taipei",
        target_date_lag_days=1,
    )

    assert before_holiday_deadline == (date(2026, 7, 1), None)
    assert at_holiday_deadline == (date(2026, 7, 2), None)
    assert after_closed_session == (date(2026, 7, 2), None)


@pytest.mark.asyncio
async def test_twelve_data_monitor_waits_until_1130_and_late_run_resolves(test_session) -> None:
    await _seed_dataset(test_session, config=_twelve_data_config())
    await _seed_published_calendar_year(test_session, market="US", year=2026)
    test_session.add(
        IngestionRun(
            dataset_key="tw_equity_eod",
            source="twelve_data",
            schema_id="market_eod",
            schema_version=1,
            batch_data_date=date(2026, 7, 21),
            delivery_mode="incremental",
            is_rerun=False,
            status="completed",
        )
    )
    await test_session.commit()

    before = await scan_missing_deliveries(
        test_session,
        now=datetime(2026, 7, 23, 3, 29, tzinfo=timezone.utc),
    )
    assert before.created_or_refreshed == 0

    due = await scan_missing_deliveries(
        test_session,
        now=datetime(2026, 7, 23, 3, 30, tzinfo=timezone.utc),
    )
    assert due.created_or_refreshed == 1
    alert = (await test_session.execute(select(MissingDeliveryAlert))).scalar_one()
    assert alert.expected_data_date == date(2026, 7, 22)

    test_session.add(
        IngestionRun(
            dataset_key="tw_equity_eod",
            source="twelve_data",
            schema_id="market_eod",
            schema_version=1,
            batch_data_date=date(2026, 7, 22),
            delivery_mode="incremental",
            is_rerun=False,
            status="completed",
        )
    )
    await test_session.commit()
    resolved = await scan_missing_deliveries(
        test_session,
        now=datetime(2026, 7, 23, 1, 16, tzinfo=timezone.utc),
    )
    assert resolved.resolved == 1
    await test_session.refresh(alert)
    assert alert.status == "resolved"


@pytest.mark.asyncio
async def test_scan_creates_once_refreshes_and_late_delivery_resolves(test_session) -> None:
    await _seed_dataset(test_session)

    first = await scan_missing_deliveries(test_session, now=NOW)
    second = await scan_missing_deliveries(test_session, now=NOW)
    assert first.created_or_refreshed == second.created_or_refreshed == 1
    assert await test_session.scalar(select(func.count()).select_from(MissingDeliveryAlert)) == 1

    await scan_missing_deliveries(
        test_session,
        now=datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc),
    )
    assert await test_session.scalar(select(func.count()).select_from(MissingDeliveryAlert)) == 2

    test_session.add(
        IngestionRun(
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            batch_data_date=date(2026, 7, 22),
            delivery_mode="full_snapshot",
            is_rerun=False,
            status="failed",
        )
    )
    await test_session.commit()
    result = await scan_missing_deliveries(test_session, now=NOW)
    alert = (
        await test_session.execute(
            select(MissingDeliveryAlert).where(
                MissingDeliveryAlert.expected_data_date == date(2026, 7, 22)
            )
        )
    ).scalar_one()
    assert result.resolved == 1
    assert alert.status == "resolved"
    assert alert.resolved_at == NOW
    still_open = await test_session.scalar(
        select(func.count())
        .select_from(MissingDeliveryAlert)
        .where(MissingDeliveryAlert.status == "open")
    )
    assert still_open == 1


@pytest.mark.asyncio
async def test_presence_requires_exact_non_rerun_full_snapshot_identity(test_session) -> None:
    await _seed_dataset(test_session)
    for source, version, data_date, mode, rerun in [
        ("other", 1, date(2026, 7, 22), "full_snapshot", False),
        ("finlab", 2, date(2026, 7, 22), "full_snapshot", False),
        ("finlab", 1, date(2026, 7, 21), "full_snapshot", False),
        ("finlab", 1, date(2026, 7, 22), "incremental", False),
        ("finlab", 1, date(2026, 7, 22), "backfill", False),
        ("finlab", 1, date(2026, 7, 22), "full_snapshot", True),
    ]:
        test_session.add(
            IngestionRun(
                dataset_key="tw_equity_eod",
                source=source,
                schema_id="market_eod",
                schema_version=version,
                batch_data_date=data_date,
                delivery_mode=mode,
                is_rerun=rerun,
                status="completed",
            )
        )
    test_session.add(
        IngestionRun(
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id=None,
            schema_version=None,
            batch_data_date=date(2026, 7, 22),
            delivery_mode="full_snapshot",
            is_rerun=False,
            status="completed",
        )
    )
    await test_session.commit()
    await scan_missing_deliveries(test_session, now=NOW)
    assert await test_session.scalar(select(func.count()).select_from(MissingDeliveryAlert)) == 1


@pytest.mark.asyncio
async def test_incremental_backfill_resolves_covered_history_and_keeps_out_of_range_alert(
    test_session,
) -> None:
    await _seed_dataset(test_session, config=_twelve_data_config())
    await _seed_published_calendar_year(test_session, market="US", year=2026)
    covered_dates = [date(2026, 7, day) for day in (20, 21, 22)]
    out_of_range = date(2026, 7, 23)
    test_session.add_all(
        [
            MissingDeliveryAlert(
                dataset_key="tw_equity_eod",
                source="twelve_data",
                schema_id="market_eod",
                schema_version=1,
                expected_data_date=data_date,
                status="open",
                first_detected_at=NOW,
                last_detected_at=NOW,
            )
            for data_date in (*covered_dates, out_of_range)
        ]
    )
    test_session.add(
        IngestionRun(
            dataset_key="tw_equity_eod",
            source="twelve_data",
            schema_id="market_eod",
            schema_version=1,
            batch_data_date=covered_dates[-1],
            delivery_mode="backfill",
            coverage_start_date=covered_dates[0],
            coverage_end_date=covered_dates[-1],
            is_rerun=False,
            status="pending",
        )
    )
    await test_session.commit()

    result = await scan_missing_deliveries(test_session, now=NOW)

    assert result.resolved == len(covered_dates)
    statuses = dict(
        (
            await test_session.execute(
                select(
                    MissingDeliveryAlert.expected_data_date,
                    MissingDeliveryAlert.status,
                )
            )
        ).all()
    )
    assert all(statuses[data_date] == "resolved" for data_date in covered_dates)
    assert statuses[out_of_range] == "open"


@pytest.mark.asyncio
async def test_incremental_backfill_without_range_only_covers_its_batch_date(test_session) -> None:
    await _seed_dataset(test_session, config=_twelve_data_config())
    await _seed_published_calendar_year(test_session, market="US", year=2026)
    expected = date(2026, 7, 21)
    other = date(2026, 7, 20)
    test_session.add_all(
        [
            MissingDeliveryAlert(
                dataset_key="tw_equity_eod",
                source="twelve_data",
                schema_id="market_eod",
                schema_version=1,
                expected_data_date=data_date,
                status="open",
                first_detected_at=NOW,
                last_detected_at=NOW,
            )
            for data_date in (other, expected)
        ]
    )
    test_session.add(
        IngestionRun(
            dataset_key="tw_equity_eod",
            source="twelve_data",
            schema_id="market_eod",
            schema_version=1,
            batch_data_date=expected,
            delivery_mode="backfill",
            is_rerun=False,
            status="pending",
        )
    )
    await test_session.commit()

    result = await scan_missing_deliveries(test_session, now=NOW)

    assert result.resolved == 1
    alerts = {
        row.expected_data_date: row.status
        for row in (await test_session.execute(select(MissingDeliveryAlert))).scalars()
    }
    assert alerts[expected] == "resolved"
    assert alerts[other] == "open"


@pytest.mark.asyncio
async def test_full_snapshot_expectation_does_not_accept_backfill_or_rerun(test_session) -> None:
    await _seed_dataset(test_session)
    expected = date(2026, 7, 22)
    test_session.add(
        MissingDeliveryAlert(
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            expected_data_date=expected,
            status="open",
            first_detected_at=NOW,
            last_detected_at=NOW,
        )
    )
    test_session.add_all(
        [
            IngestionRun(
                dataset_key="tw_equity_eod",
                source="finlab",
                schema_id="market_eod",
                schema_version=1,
                batch_data_date=expected,
                delivery_mode="backfill",
                is_rerun=is_rerun,
                status="pending",
            )
            for is_rerun in (False, True)
        ]
    )
    await test_session.commit()

    result = await scan_missing_deliveries(test_session, now=NOW)

    assert result.resolved == 0
    alert = (await test_session.execute(select(MissingDeliveryAlert))).scalar_one()
    assert alert.status == "open"


@pytest.mark.asyncio
async def test_disabled_inactive_and_calendar_unavailable_do_not_alert(test_session) -> None:
    await _seed_dataset(test_session, config=_config(action="disabled"))
    result = await scan_missing_deliveries(test_session, now=NOW)
    assert result.created_or_refreshed == 0

    test_session.add(
        MissingDeliveryAlert(
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            expected_data_date=date(2026, 7, 21),
            status="open",
            first_detected_at=NOW,
            last_detected_at=NOW,
        )
    )
    await test_session.commit()
    await scan_missing_deliveries(test_session, now=NOW)
    assert (
        await test_session.scalar(select(func.count()).where(MissingDeliveryAlert.status == "open"))
        == 1
    )

    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    assert dataset is not None
    dataset.config = _config(sources=["finlab"])
    dataset.is_active = False
    await test_session.commit()
    assert (await scan_missing_deliveries(test_session, now=NOW)).created_or_refreshed == 0
    assert (
        await test_session.scalar(select(func.count()).where(MissingDeliveryAlert.status == "open"))
        == 1
    )

    dataset.is_active = True
    await test_session.delete(
        (await test_session.execute(select(CalendarYearRevision))).scalar_one()
    )
    await test_session.commit()
    unavailable = await scan_missing_deliveries(test_session, now=NOW)
    assert unavailable.created_or_refreshed == 0
    assert unavailable.diagnostics[0]["reason"] == "calendar_does_not_cover_evaluation_date"


@pytest.mark.asyncio
async def test_admin_list_filters_paginates_and_health_summarizes(
    client, test_session, admin_headers
) -> None:
    await _seed_dataset(test_session)
    await scan_missing_deliveries(test_session, now=NOW)

    unauthorized = await client.get("/api/v1/admin/missing-deliveries")
    assert unauthorized.status_code == 401
    response = await client.get(
        "/api/v1/admin/missing-deliveries",
        params={"status": "open", "dataset_key": "tw_equity_eod", "source": "FINLAB"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["total_records"] == 1
    assert body["data"][0]["source"] == "finlab"

    health = await client.get("/api/v1/admin/queue/health", headers=admin_headers)
    assert health.status_code == 200
    assert health.json()["missing_deliveries"] == 1
    assert health.json()["oldest_missing_delivery_at"] == NOW.isoformat().replace("+00:00", "Z")

    test_session.add(
        MissingDeliveryAlert(
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            expected_data_date=date(2026, 7, 21),
            status="open",
            first_detected_at=NOW,
            last_detected_at=NOW,
        )
    )
    await test_session.commit()
    first_page = await client.get(
        "/api/v1/admin/missing-deliveries",
        params={"page": 1, "page_size": 1},
        headers=admin_headers,
    )
    second_page = await client.get(
        "/api/v1/admin/missing-deliveries",
        params={"page": 2, "page_size": 1},
        headers=admin_headers,
    )
    assert first_page.json()["pagination"] == {
        "page": 1,
        "page_size": 1,
        "total_records": 2,
        "total_pages": 2,
        "next_cursor": None,
    }
    assert len(first_page.json()["data"]) == len(second_page.json()["data"]) == 1
    assert first_page.json()["data"][0]["alert_id"] != second_page.json()["data"][0]["alert_id"]


@pytest.mark.asyncio
async def test_concurrent_scans_cannot_create_duplicate_alerts(test_engine) -> None:
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        await _seed_dataset(session)

    async def scan() -> None:
        async with session_factory() as session:
            await scan_missing_deliveries(session, now=NOW)

    await asyncio.gather(scan(), scan())
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(MissingDeliveryAlert)) == 1


async def _accept_full_snapshot(session_factory) -> None:
    async with session_factory() as session:
        await lock_feed_scope(
            session,
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            wait=True,
        )
        session.add(
            IngestionRun(
                dataset_key="tw_equity_eod",
                source="finlab",
                schema_id="market_eod",
                schema_version=1,
                batch_data_date=date(2026, 7, 22),
                delivery_mode="full_snapshot",
                is_rerun=False,
                status="pending",
            )
        )
        await session.flush()
        await resolve_missing_delivery_for_run(
            session,
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            data_date=date(2026, 7, 22),
            resolved_at=NOW,
        )
        await session.commit()


@pytest.mark.asyncio
async def test_scan_first_then_ingest_resolves_without_false_open(test_engine, monkeypatch) -> None:
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        await _seed_dataset(session)

    scan_holds_lock = asyncio.Event()
    release_scan = asyncio.Event()
    original_resolver = resolve_expected_data_date

    async def paused_resolver(db, policy, now):
        scan_holds_lock.set()
        await release_scan.wait()
        return await original_resolver(db, policy, now)

    monkeypatch.setattr("app.services.delivery_monitor.resolve_expected_data_date", paused_resolver)
    async with session_factory() as scan_session:
        scan_task = asyncio.create_task(scan_missing_deliveries(scan_session, now=NOW))
        await asyncio.wait_for(scan_holds_lock.wait(), timeout=2)
        ingest_task = asyncio.create_task(_accept_full_snapshot(session_factory))
        await asyncio.sleep(0.05)
        assert not ingest_task.done()
        release_scan.set()
        await asyncio.gather(scan_task, ingest_task)

    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count()).where(MissingDeliveryAlert.status == "open"))
            == 0
        )


@pytest.mark.asyncio
async def test_ingest_first_makes_concurrent_scan_skip_then_no_false_open(test_engine) -> None:
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        await _seed_dataset(session)

    ingest_holds_lock = asyncio.Event()
    release_ingest = asyncio.Event()

    async def paused_ingest() -> None:
        async with session_factory() as session:
            await lock_feed_scope(
                session,
                dataset_key="tw_equity_eod",
                source="finlab",
                schema_id="market_eod",
                schema_version=1,
                wait=True,
            )
            ingest_holds_lock.set()
            await release_ingest.wait()
            session.add(
                IngestionRun(
                    dataset_key="tw_equity_eod",
                    source="finlab",
                    schema_id="market_eod",
                    schema_version=1,
                    batch_data_date=date(2026, 7, 22),
                    delivery_mode="full_snapshot",
                    is_rerun=False,
                    status="pending",
                )
            )
            await session.commit()

    ingest_task = asyncio.create_task(paused_ingest())
    await asyncio.wait_for(ingest_holds_lock.wait(), timeout=2)
    async with session_factory() as scan_session:
        result = await scan_missing_deliveries(scan_session, now=NOW)
    assert result.skipped_locked is True
    release_ingest.set()
    await ingest_task

    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count()).where(MissingDeliveryAlert.status == "open"))
            == 0
        )
