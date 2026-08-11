"""Acceptance coverage for sequenced Shioaji minute freshness semantics."""

from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models.canonical import CalendarMarket, CalendarRevisionDay, CalendarYearRevision
from app.models.registry import (
    DatasetRegistry,
    IngestionRun,
    MissingDeliveryAlert,
    SchedulerControl,
    SchedulerDataset,
)
from app.services.delivery_monitor import scan_missing_deliveries
from app.services.delivery_policy import DeliveryExpectation, resolve_expected_data_date
from app.services.market_freshness import list_market_freshness
from app.utils import uuid7

DATA_DATE = date(2026, 7, 22)
PRIOR_DATA_DATE = date(2026, 7, 21)
BEFORE_DUE = datetime(2026, 7, 22, 8, 59, tzinfo=timezone.utc)  # 16:59 Asia/Taipei
AT_DUE = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)  # 17:00 Asia/Taipei


def _minute_config(asset_class: str) -> dict:
    return {
        "schema_id": "market_minute",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "audit",
        "defaults": {
            "market": "TW",
            "asset_class": asset_class,
            "currency": "TWD",
            "market_timezone": "Asia/Taipei",
            "price_adjustment": "none",
        },
        "delivery_expectation": {
            "delivery_mode": "sequenced_snapshot",
            "baseline": {"enabled": False},
            "record_count": {
                "minimum_record_count": 0,
                "maximum_count_drop_ratio": 0.0,
                "action": "disabled",
            },
            "freshness": {
                "maximum_fetch_age_hours": 6,
                "allowed_clock_skew_minutes": 5,
                "action": "warn",
            },
            "latest_date": {
                "calendar_market": "TW",
                "timezone": "Asia/Taipei",
                "market_close_time": "13:30:00",
                "availability_grace_minutes": 60,
                "action": "warn",
            },
            "missing_delivery": {
                "action": "warn",
                "expected_sources": ["shioaji"],
                "deadline_local_time": "17:00:00",
            },
            "schedule": {
                "enabled": True,
                "slot_id": "taiwan_market_window",
                "local_time": "14:30:00",
                "timezone": "Asia/Taipei",
                "expected_sources": ["shioaji"],
            },
        },
    }


async def _seed_calendar(session) -> None:
    session.add(
        CalendarMarket(
            market="TW",
            display_name="Taiwan",
            timezone="Asia/Taipei",
            weekend_days=[5, 6],
        )
    )
    revision = CalendarYearRevision(
        market="TW",
        year=2026,
        revision=1,
        status="published",
        expected_days=365,
        actual_days=365,
        timezone="Asia/Taipei",
        source_kind="test",
    )
    session.add(revision)
    await session.flush()
    start = date(2026, 1, 1)
    session.add_all(
        [
            CalendarRevisionDay(
                calendar_revision_id=revision.id,
                trade_date=start + timedelta(days=offset),
                is_open=(start + timedelta(days=offset)).weekday() < 5,
                day_status=("open" if (start + timedelta(days=offset)).weekday() < 5 else "closed"),
                source_kind="test",
            )
            for offset in range(365)
        ]
    )


async def _seed_minute_datasets(
    session,
    dataset_keys: tuple[str, ...] = ("tw_equity_minute", "tw_etf_minute"),
) -> None:
    definitions = {
        "tw_equity_minute": ("TW equity minute", "equity"),
        "tw_etf_minute": ("TW ETF minute", "etf"),
    }
    session.add_all(
        [
            DatasetRegistry(
                dataset_key=dataset_key,
                name=definitions[dataset_key][0],
                asset_class=definitions[dataset_key][1],
                market="TW",
                frequency="minute",
                is_active=True,
                config=_minute_config(definitions[dataset_key][1]),
            )
            for dataset_key in dataset_keys
        ]
    )
    scheduler_key = "shioaji_tw_pilot_v1"
    session.add(
        SchedulerControl(
            scheduler_key=scheduler_key,
            provider="shioaji",
            slot_id="taiwan_market_window",
            scheduled_local_time=time(14, 30),
            timezone="Asia/Taipei",
            dataset_keys=list(dataset_keys),
            desired_state="running",
            observed_state="running",
            revision=1,
        )
    )
    await _seed_calendar(session)
    await session.flush()
    session.add_all(
        [
            SchedulerDataset(scheduler_key=scheduler_key, dataset_key=dataset_key)
            for dataset_key in dataset_keys
        ]
    )
    await session.commit()


def _run(
    dataset_key: str,
    sequence: int,
    sequence_count: int,
    *,
    snapshot_id: str = "snapshot-a",
    daily_update_id: str = "update-a",
    status: str = "completed",
    data_date: date = DATA_DATE,
    attempt: int = 0,
) -> IngestionRun:
    created_at = datetime.combine(data_date, datetime.min.time(), tzinfo=timezone.utc).replace(
        hour=9, minute=sequence + attempt * 10
    )
    return IngestionRun(
        run_id=uuid7(),
        dataset_key=dataset_key,
        source="shioaji",
        schema_id="market_minute",
        schema_version=1,
        batch_data_date=data_date,
        delivery_mode="sequenced_snapshot",
        snapshot_id=snapshot_id,
        daily_update_id=daily_update_id,
        sequence=sequence,
        sequence_count=sequence_count,
        is_rerun=False,
        status=status,
        completed_at=created_at if status == "completed" else None,
        created_at=created_at,
        failure_code="NORMALIZATION_FAILED" if status == "failed" else None,
        raw_records=1,
        total_records=1,
        success_records=1 if status == "completed" else 0,
    )


@pytest.mark.asyncio
async def test_minute_freshness_requires_one_complete_coherent_group(test_session) -> None:
    await _seed_minute_datasets(test_session)
    test_session.add_all(
        [
            _run("tw_etf_minute", 1, 3),
            _run("tw_etf_minute", 2, 3),
        ]
    )
    await test_session.commit()

    partial = (await list_market_freshness(test_session, market="TW", now=AT_DUE))[0]
    etf = next(feed for feed in partial.feeds if feed.dataset_key == "tw_etf_minute")
    assert etf.status != "fresh"
    assert partial.status != "fresh"

    test_session.add(_run("tw_etf_minute", 3, 3))
    await test_session.commit()
    complete = (await list_market_freshness(test_session, market="TW", now=AT_DUE))[0]
    etf = next(feed for feed in complete.feeds if feed.dataset_key == "tw_etf_minute")
    assert etf.status == "fresh"

    # An older incomplete sequence must not downgrade the current expected
    # date after a complete group has been observed.
    test_session.add_all(
        [_run("tw_etf_minute", sequence, 3, data_date=PRIOR_DATA_DATE) for sequence in (1, 2)]
    )
    await test_session.commit()
    remains_fresh = (await list_market_freshness(test_session, market="TW", now=AT_DUE))[0]
    etf = next(feed for feed in remains_fresh.feeds if feed.dataset_key == "tw_etf_minute")
    assert etf.status == "fresh"


@pytest.mark.asyncio
async def test_minute_freshness_does_not_stitch_mixed_snapshots_and_recovers_failure(
    test_session,
) -> None:
    await _seed_minute_datasets(test_session)
    test_session.add_all(
        [
            _run("tw_equity_minute", 1, 3, snapshot_id="snapshot-a"),
            _run("tw_equity_minute", 2, 3, snapshot_id="snapshot-b"),
            _run("tw_equity_minute", 3, 3, snapshot_id="snapshot-a"),
        ]
    )
    await test_session.commit()
    mixed = (await list_market_freshness(test_session, market="TW", now=AT_DUE))[0]
    equity = next(feed for feed in mixed.feeds if feed.dataset_key == "tw_equity_minute")
    assert equity.status != "fresh"

    test_session.add(_run("tw_equity_minute", 2, 3, status="failed", attempt=1))
    await test_session.commit()
    failed = (await list_market_freshness(test_session, market="TW", now=AT_DUE))[0]
    equity = next(feed for feed in failed.feeds if feed.dataset_key == "tw_equity_minute")
    assert equity.status == "failed"

    test_session.add(_run("tw_equity_minute", 2, 3, attempt=2))
    await test_session.commit()
    recovered = (await list_market_freshness(test_session, market="TW", now=AT_DUE))[0]
    equity = next(feed for feed in recovered.feeds if feed.dataset_key == "tw_equity_minute")
    assert equity.status == "fresh"


@pytest.mark.asyncio
async def test_minute_freshness_uses_latest_attempt_per_sequence(test_session) -> None:
    await _seed_minute_datasets(test_session, ("tw_equity_minute",))
    test_session.add_all([_run("tw_equity_minute", sequence, 3) for sequence in (1, 2, 3)])
    await test_session.commit()
    initial = (await list_market_freshness(test_session, market="TW", now=AT_DUE))[0]
    assert initial.feeds[0].status == "fresh"

    # A newer failed retry must hide the older completed sequence attempt.
    test_session.add(_run("tw_equity_minute", 2, 3, status="failed", attempt=1))
    await test_session.commit()
    failed = (await list_market_freshness(test_session, market="TW", now=AT_DUE))[0]
    assert failed.feeds[0].status == "failed"

    # A still newer completed retry restores the same coherent group.
    test_session.add(_run("tw_equity_minute", 2, 3, attempt=2))
    await test_session.commit()
    recovered = (await list_market_freshness(test_session, market="TW", now=AT_DUE))[0]
    assert recovered.feeds[0].status == "fresh"


@pytest.mark.asyncio
async def test_minute_monitor_waits_until_due_and_resolves_only_complete_group(
    test_session,
) -> None:
    await _seed_minute_datasets(test_session, ("tw_equity_minute",))
    test_session.add_all(
        [_run("tw_equity_minute", sequence, 3, data_date=PRIOR_DATA_DATE) for sequence in (1, 2, 3)]
        + [_run("tw_equity_minute", 1, 3)]
    )
    await test_session.commit()

    before = await scan_missing_deliveries(test_session, now=BEFORE_DUE)
    assert before.created_or_refreshed == 0
    assert await test_session.scalar(select(func.count()).select_from(MissingDeliveryAlert)) == 0

    due = await scan_missing_deliveries(test_session, now=AT_DUE)
    assert due.created_or_refreshed == 1
    assert await test_session.scalar(select(func.count()).select_from(MissingDeliveryAlert)) == 1

    test_session.add_all([_run("tw_equity_minute", 2, 3), _run("tw_equity_minute", 3, 3)])
    await test_session.commit()
    resolved = await scan_missing_deliveries(test_session, now=AT_DUE)
    assert resolved.resolved == 1
    assert (
        await test_session.scalar(
            select(func.count())
            .select_from(MissingDeliveryAlert)
            .where(MissingDeliveryAlert.status == "open")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_minute_market_freshness_uses_operational_deadline(test_session) -> None:
    await _seed_minute_datasets(test_session, ("tw_equity_minute",))
    test_session.add_all(
        [_run("tw_equity_minute", sequence, 3, data_date=PRIOR_DATA_DATE) for sequence in (1, 2, 3)]
    )
    await test_session.commit()

    before = (await list_market_freshness(test_session, market="TW", now=BEFORE_DUE))[0]
    assert before.expected_data_date == PRIOR_DATA_DATE
    assert before.feeds[0].status == "fresh"

    at_deadline = (await list_market_freshness(test_session, market="TW", now=AT_DUE))[0]
    assert at_deadline.expected_data_date == DATA_DATE
    assert at_deadline.feeds[0].status == "late"


@pytest.mark.asyncio
async def test_minute_ingest_and_monitor_deadlines_are_independent(test_session) -> None:
    await _seed_minute_datasets(test_session, ("tw_equity_minute",))
    expectation = DeliveryExpectation.model_validate(
        _minute_config("equity")["delivery_expectation"]
    )
    assert expectation.latest_date is not None
    at_ingest_due = datetime(2026, 7, 22, 6, 30, tzinfo=timezone.utc)

    ingest_expected, ingest_reason = await resolve_expected_data_date(
        test_session,
        expectation.latest_date,
        at_ingest_due,
        strict_current_session=True,
    )
    monitor_expected, monitor_reason = await resolve_expected_data_date(
        test_session,
        expectation.latest_date,
        at_ingest_due,
        strict_current_session=True,
        current_session_deadline=time(17),
    )

    assert (ingest_expected, ingest_reason) == (DATA_DATE, None)
    assert (monitor_expected, monitor_reason) == (PRIOR_DATA_DATE, None)
