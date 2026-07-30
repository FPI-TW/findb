"""Delivery policy parsing, baseline, mode, and calendar behavior."""

from datetime import date, datetime, time, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import (
    CalendarMarket,
    CalendarRevisionDay,
    CalendarYearRevision,
    TradingCalendar,
)
from app.models.registry import DatasetRegistry, IngestionRun
from app.schemas.ingress import DeliveryMode
from app.services.delivery_policy import (
    DeliveryExpectation,
    PolicyAction,
    evaluate_delivery_policy,
)
from app.services.ingress_contracts import validate_ingress_request
from app.utils import uuid7
from scripts.seed_data import seed_datasets


def _request(
    *,
    count: int = 1,
    data_date: date = date(2026, 7, 21),
    mode: str = "full_snapshot",
    source: str = "finlab",
    fetched_at: datetime = datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
):
    rows = [
        {
            "symbol": f"{index:04d}",
            "trade_date": data_date.isoformat(),
            "currency": "TWD",
            "close": "100",
        }
        for index in range(count)
    ]
    return validate_ingress_request(
        {
            "dataset_key": "tw_equity_eod",
            "schema_id": "market_eod",
            "schema_version": 1,
            "source": source,
            "request_key": f"request-{source}-{data_date}",
            "idempotency_key": f"idem-{source}-{data_date}",
            "fetched_at": fetched_at.isoformat(),
            "payload": {
                "batch": {
                    "data_date": data_date.isoformat(),
                    "delivery_mode": mode,
                    "declared_record_count": count,
                },
                "data": rows,
            },
        }
    )


def _expectation(
    *,
    count_action: str = "warn",
    freshness_action: str = "warn",
    latest_action: str | None = "disabled",
    minimum: int = 0,
    ratio: float = 0.1,
) -> DeliveryExpectation:
    value = {
        "delivery_mode": "full_snapshot",
        "baseline": {"window_size": 7, "minimum_history": 3},
        "record_count": {
            "minimum_record_count": minimum,
            "maximum_count_drop_ratio": ratio,
            "action": count_action,
        },
        "freshness": {
            "maximum_fetch_age_hours": 36,
            "allowed_clock_skew_minutes": 5,
            "action": freshness_action,
        },
    }
    if latest_action:
        value["latest_date"] = {
            "calendar_market": "TW",
            "timezone": "Asia/Taipei",
            "market_close_time": "13:30:00",
            "availability_grace_minutes": 120,
            "action": latest_action,
        }
    return DeliveryExpectation.model_validate(value)


async def _add_published_calendar_year(
    session: AsyncSession, *, market: str, year: int, overrides: dict[date, dict] | None = None
) -> None:
    if await session.get(CalendarMarket, market) is None:
        session.add(
            CalendarMarket(
                market=market,
                display_name=market,
                timezone="Asia/Taipei",
                weekend_days=[5, 6],
            )
        )
        await session.flush()
    start = date(year, 1, 1)
    days = (date(year, 12, 31) - start).days + 1
    revision = CalendarYearRevision(
        market=market,
        year=year,
        revision=1,
        status="published",
        expected_days=days,
        actual_days=days,
        timezone="Asia/Taipei",
        source_kind="manual",
    )
    session.add(revision)
    await session.flush()
    overrides = overrides or {}
    for offset in range(days):
        trade_date = start + timedelta(days=offset)
        value = {
            "is_open": trade_date.weekday() < 5,
            "day_status": "open" if trade_date.weekday() < 5 else "closed",
        }
        value.update(overrides.get(trade_date, {}))
        session.add(
            CalendarRevisionDay(
                calendar_revision_id=revision.id,
                trade_date=trade_date,
                source_kind="manual",
                **value,
            )
        )
    await session.commit()


def test_flat_delivery_expectation_is_backward_compatible() -> None:
    policy = DeliveryExpectation.model_validate(
        {
            "delivery_mode": "full_snapshot",
            "freshness_hours": 48,
            "minimum_record_count": 123,
            "maximum_count_drop_ratio": 0.25,
        }
    )

    assert policy.record_count.minimum_record_count == 123
    assert policy.record_count.maximum_count_drop_ratio == 0.25
    assert policy.record_count.action == PolicyAction.WARN
    assert policy.freshness.maximum_fetch_age_hours == 48


@pytest.mark.parametrize(
    "value",
    [
        {"baseline": {"window_size": 2}},
        {"baseline": {"window_size": 7, "minimum_history": 8}},
        {"record_count": {"maximum_count_drop_ratio": 1}},
        {"record_count": {"action": "block"}},
        {"freshness": {"allowed_clock_skew_minutes": 61}},
        {
            "latest_date": {
                "calendar_market": "TW",
                "timezone": "Not/AZone",
                "market_close_time": "13:30:00",
            }
        },
    ],
)
def test_invalid_delivery_expectation_is_rejected(value: dict) -> None:
    with pytest.raises(ValidationError):
        DeliveryExpectation.model_validate(value)


@pytest.mark.asyncio
async def test_dataset_seed_preserves_existing_operator_config(
    test_session: AsyncSession,
) -> None:
    test_session.add(
        DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="Operator Name",
            asset_class="equity",
            market="TW",
            frequency="daily",
            is_active=True,
            config={
                "operator_note": "preserve",
                "delivery_expectation": {
                    "delivery_mode": "full_snapshot",
                    "minimum_record_count": 1777,
                    "freshness_hours": 72,
                },
            },
        )
    )
    await test_session.commit()

    await seed_datasets(test_session)

    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    assert dataset.config["operator_note"] == "preserve"
    assert dataset.config["delivery_expectation"]["minimum_record_count"] == 1777
    assert dataset.config["delivery_expectation"]["freshness_hours"] == 72
    assert dataset.config["delivery_expectation"]["missing_delivery"] == {
        "action": "disabled",
        "expected_sources": [],
    }
    assert dataset.config["schema_id"] == "market_eod"

    dataset.config = {
        **dataset.config,
        "delivery_expectation": {
            **dataset.config["delivery_expectation"],
            "missing_delivery": {
                "action": "warn",
                "expected_sources": ["operator_feed"],
            },
        },
    }
    await test_session.commit()
    await seed_datasets(test_session)
    await test_session.refresh(dataset)
    assert dataset.config["delivery_expectation"]["missing_delivery"] == {
        "action": "warn",
        "expected_sources": ["operator_feed"],
    }


async def _seed_dataset(session: AsyncSession) -> None:
    session.add(
        DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="TW Equity",
            asset_class="equity",
            market="TW",
            frequency="daily",
            is_active=True,
            config={},
        )
    )
    await session.flush()


async def _add_baseline(
    session: AsyncSession,
    *,
    data_date: date,
    count: int,
    source: str = "finlab",
    status: str = "completed",
    outcome: str = "pass",
    mode: str = "full_snapshot",
    is_rerun: bool = False,
) -> IngestionRun:
    run = IngestionRun(
        run_id=uuid7(),
        dataset_key="tw_equity_eod",
        source=source,
        schema_id="market_eod",
        schema_version=1,
        batch_data_date=data_date,
        delivery_mode=mode,
        policy_outcome=outcome,
        is_rerun=is_rerun,
        raw_records=count,
        status=status,
    )
    session.add(run)
    await session.flush()
    return run


@pytest.mark.asyncio
async def test_baseline_uses_median_ceil_and_excludes_ineligible_history(
    test_session: AsyncSession,
) -> None:
    await _seed_dataset(test_session)
    eligible = [
        await _add_baseline(test_session, data_date=date(2026, 7, day), count=count)
        for day, count in [(16, 100), (17, 101), (18, 102)]
    ]
    await _add_baseline(test_session, data_date=date(2026, 7, 15), count=500, source="bloomberg")
    await _add_baseline(test_session, data_date=date(2026, 7, 14), count=500, outcome="warn")
    await _add_baseline(test_session, data_date=date(2026, 7, 13), count=500, is_rerun=True)
    await _add_baseline(test_session, data_date=date(2026, 7, 12), count=500, mode="incremental")
    await _add_baseline(
        test_session, data_date=date(2026, 7, 11), count=500, status="completed_with_errors"
    )
    await test_session.commit()

    result = await evaluate_delivery_policy(
        test_session,
        _request(count=90),
        _expectation(minimum=50, ratio=0.1),
        now=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
    )

    assert result.outcome == "warn"
    assert result.baseline_status == "active"
    assert result.baseline_median == 101
    assert result.effective_count_threshold == 91
    assert result.baseline_run_ids == [run.run_id for run in reversed(eligible)]
    assert result.violations[0].reason == "relative_drop"


@pytest.mark.asyncio
async def test_cold_start_uses_absolute_minimum_only(test_session: AsyncSession) -> None:
    await _seed_dataset(test_session)
    await _add_baseline(test_session, data_date=date(2026, 7, 20), count=1_000)
    await test_session.commit()

    result = await evaluate_delivery_policy(
        test_session,
        _request(count=80),
        _expectation(minimum=75, ratio=0.1),
        now=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
    )

    assert result.outcome == "pass"
    assert result.baseline_status == "cold_start"
    assert result.effective_count_threshold == 75


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_codes"),
    [
        (DeliveryMode.FULL_SNAPSHOT.value, {"BATCH_RECORD_COUNT_DROP", "STALE_PAYLOAD"}),
        (DeliveryMode.INCREMENTAL.value, {"STALE_PAYLOAD"}),
        (DeliveryMode.BACKFILL.value, set()),
    ],
)
async def test_delivery_mode_policy_matrix(
    test_session: AsyncSession,
    mode: str,
    expected_codes: set[str],
) -> None:
    result = await evaluate_delivery_policy(
        test_session,
        _request(
            count=1,
            mode=mode,
            fetched_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
        _expectation(minimum=2),
        now=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
    )

    assert {item.code for item in result.violations} == expected_codes
    assert result.baseline_status == (
        "cold_start" if mode == DeliveryMode.FULL_SNAPSHOT.value else "not_applicable"
    )


@pytest.mark.asyncio
async def test_future_fetched_at_is_stale_payload_violation(test_session: AsyncSession) -> None:
    now = datetime(2026, 7, 21, 8, tzinfo=timezone.utc)
    result = await evaluate_delivery_policy(
        test_session,
        _request(mode="incremental", fetched_at=now + timedelta(minutes=6)),
        _expectation(),
        now=now,
    )

    assert result.primary_code == "STALE_PAYLOAD"
    assert result.violations[0].reason == "future_clock"


@pytest.mark.asyncio
async def test_calendar_close_grace_and_holiday_resolution(test_session: AsyncSession) -> None:
    await _add_published_calendar_year(
        test_session,
        market="TW",
        year=2026,
        overrides={
            date(2026, 7, 17): {
                "is_open": True,
                "day_status": "open",
                "session_close": time(13, 30),
            },
            date(2026, 7, 18): {"is_open": False, "day_status": "closed"},
            date(2026, 7, 19): {"is_open": False, "day_status": "closed"},
            date(2026, 7, 20): {
                "is_open": True,
                "day_status": "open",
                "session_close": time(13, 30),
            },
            date(2026, 7, 21): {
                "is_open": True,
                "day_status": "open",
                "session_close": time(13, 30),
            },
        },
    )
    test_session.add_all(
        [
            TradingCalendar(
                market="TW", trade_date=date(2026, 7, 17), is_open=True, session_close=time(13, 30)
            ),
            TradingCalendar(
                market="TW",
                trade_date=date(2026, 7, 18),
                is_open=False,
                day_status="closed",
            ),
            TradingCalendar(
                market="TW",
                trade_date=date(2026, 7, 19),
                is_open=False,
                day_status="closed",
            ),
            TradingCalendar(
                market="TW", trade_date=date(2026, 7, 20), is_open=True, session_close=time(13, 30)
            ),
            TradingCalendar(
                market="TW", trade_date=date(2026, 7, 21), is_open=True, session_close=time(13, 30)
            ),
        ]
    )
    await test_session.commit()
    policy = _expectation(latest_action="reject")

    weekend = await evaluate_delivery_policy(
        test_session,
        _request(
            data_date=date(2026, 7, 17),
            fetched_at=datetime(2026, 7, 19, 4, tzinfo=timezone.utc),
        ),
        policy,
        now=datetime(2026, 7, 19, 4, tzinfo=timezone.utc),
    )
    before_grace = await evaluate_delivery_policy(
        test_session,
        _request(data_date=date(2026, 7, 20)),
        policy,
        now=datetime(2026, 7, 21, 7, tzinfo=timezone.utc),  # 15:00 local
    )
    after_grace = await evaluate_delivery_policy(
        test_session,
        _request(data_date=date(2026, 7, 20)),
        policy,
        now=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),  # 16:00 local
    )

    assert "LATEST_DATE_MISSING" not in {item.code for item in weekend.violations}
    assert "LATEST_DATE_MISSING" not in {item.code for item in before_grace.violations}
    assert after_grace.primary_code == "LATEST_DATE_MISSING"
    assert after_grace.outcome == "reject"


@pytest.mark.asyncio
async def test_calendar_freshness_ignores_observations_and_crosses_year_boundary(
    test_session: AsyncSession,
) -> None:
    await _add_published_calendar_year(
        test_session,
        market="TW",
        year=2025,
        overrides={
            date(2025, 12, 31): {
                "is_open": True,
                "day_status": "open",
                "session_close": time(13, 30),
            }
        },
    )
    await _add_published_calendar_year(
        test_session,
        market="TW",
        year=2026,
        overrides={
            date(2026, 1, 1): {"is_open": False, "day_status": "closed"},
            date(2026, 1, 2): {"is_open": False, "day_status": "closed"},
        },
    )
    test_session.add(TradingCalendar(market="TW", trade_date=date(2026, 1, 1), is_open=True))
    await test_session.commit()

    result = await evaluate_delivery_policy(
        test_session,
        _request(data_date=date(2025, 12, 31)),
        _expectation(latest_action="reject"),
        now=datetime(2026, 1, 2, 8, tzinfo=timezone.utc),
    )

    assert "LATEST_DATE_MISSING" not in {item.code for item in result.violations}


@pytest.mark.asyncio
async def test_calendar_freshness_does_not_leak_wtx_or_use_tw_draft_observations(
    test_session: AsyncSession,
) -> None:
    await _add_published_calendar_year(test_session, market="WTX", year=2026)
    test_session.add_all(
        [
            CalendarMarket(
                market="TW",
                display_name="TW",
                timezone="Asia/Taipei",
                weekend_days=[5, 6],
            ),
            CalendarYearRevision(
                market="TW",
                year=2026,
                revision=1,
                status="draft",
                expected_days=365,
                actual_days=365,
                timezone="Asia/Taipei",
                source_kind="manual",
            ),
            TradingCalendar(market="TW", trade_date=date(2026, 7, 21), is_open=True),
        ]
    )
    await test_session.commit()

    result = await evaluate_delivery_policy(
        test_session,
        _request(data_date=date(2026, 7, 20)),
        _expectation(latest_action="reject"),
        now=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
    )

    assert result.primary_code == "CALENDAR_UNAVAILABLE"
    assert result.violations[0].reason == "calendar_does_not_cover_evaluation_date"


@pytest.mark.asyncio
async def test_calendar_freshness_rejects_incomplete_published_tw_revision(
    test_session: AsyncSession,
) -> None:
    test_session.add_all(
        [
            CalendarMarket(
                market="TW",
                display_name="TW",
                timezone="Asia/Taipei",
                weekend_days=[5, 6],
            ),
            CalendarYearRevision(
                market="TW",
                year=2026,
                revision=1,
                status="published",
                expected_days=365,
                actual_days=364,
                timezone="Asia/Taipei",
                source_kind="manual",
            ),
            TradingCalendar(market="TW", trade_date=date(2026, 7, 21), is_open=True),
        ]
    )
    await test_session.commit()

    result = await evaluate_delivery_policy(
        test_session,
        _request(data_date=date(2026, 7, 20)),
        _expectation(latest_action="reject"),
        now=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
    )

    assert result.primary_code == "CALENDAR_UNAVAILABLE"
    assert result.violations[0].reason == "calendar_does_not_cover_evaluation_date"


@pytest.mark.asyncio
async def test_missing_calendar_is_warning_even_when_latest_rule_rejects(
    test_session: AsyncSession,
) -> None:
    result = await evaluate_delivery_policy(
        test_session,
        _request(),
        _expectation(latest_action="reject"),
        now=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
    )

    assert result.outcome == "warn"
    assert result.primary_code == "CALENDAR_UNAVAILABLE"
    assert result.violations[0].action == "warn"


@pytest.mark.asyncio
async def test_missing_latest_date_config_is_calendar_warning(
    test_session: AsyncSession,
) -> None:
    result = await evaluate_delivery_policy(
        test_session,
        _request(),
        _expectation(latest_action=None),
        now=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
    )

    assert result.outcome == "warn"
    assert result.primary_code == "CALENDAR_UNAVAILABLE"
    assert result.violations[0].reason == "latest_date_policy_not_configured"
