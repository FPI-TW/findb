"""Effective calendar defaults and frozen profile decisions."""

from datetime import date, datetime, time, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select

from app.config import get_settings
from app.models.canonical import CalendarRevisionDay, CalendarYearRevision
from app.models.registry import DailyDeliveryPlan, DailyExecutionDecision, FullMarketEnvironment
from app.schemas.full_market import DeliveryPlanCreateRequest
from app.services.effective_calendar import MARKETS, PROVIDER_CALENDAR_SWITCHES, effective_days
from app.services.full_market import create_plan
from app.services.full_market_admission import eligibility
from app.services.historical_backfill import create_request
from tests.full_market_fixtures import historical_admission
from tests.owned_history import claim_next_item
from tests.test_full_market import DAY, _baseline, _dataset
from tests.test_historical_backfill import _seed_scope


@pytest.mark.parametrize("provider", PROVIDER_CALENDAR_SWITCHES)
def test_provider_switch_defaults_and_independence(monkeypatch, provider):
    from app.config import Settings
    from app.services.effective_calendar import execution_policy

    for key in PROVIDER_CALENDAR_SWITCHES.values():
        monkeypatch.delenv(key, raising=False)
    defaults = Settings(_env_file=None)
    assert all(getattr(defaults, key) is True for key in PROVIDER_CALENDAR_SWITCHES.values())
    for key in PROVIDER_CALENDAR_SWITCHES.values():
        monkeypatch.setenv(key, "false")
    monkeypatch.setenv(PROVIDER_CALENDAR_SWITCHES[provider], "true")
    configured = Settings(_env_file=None)
    for name, key in PROVIDER_CALENDAR_SWITCHES.items():
        assert getattr(configured, key) is (name == provider)
        monkeypatch.setattr(get_settings(), key, getattr(configured, key))
    assert {
        name: execution_policy(name)["skip_non_trading_days"] for name in PROVIDER_CALENDAR_SWITCHES
    } == {name: name == provider for name in PROVIDER_CALENDAR_SWITCHES}


@pytest.mark.parametrize("key", PROVIDER_CALENDAR_SWITCHES.values())
@pytest.mark.parametrize("value", ["yes", "1", "{}", "TRUE"])
def test_provider_switch_rejects_invalid_values(monkeypatch, key, value):
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.setenv(key, value)
    with pytest.raises(ValidationError, match="provider calendar switch"):
        Settings(_env_file=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("market", MARKETS)
async def test_defaults_cover_leap_year_without_writing(test_session, market):
    rows = await effective_days(test_session, market, date(2024, 1, 1), date(2024, 12, 31))
    assert len(rows) == 366
    for row in rows:
        assert row["is_open"] == (date.fromisoformat(row["trade_date"]).weekday() < 5)
        assert row["calendar_source"] == "weekday_default"
        assert row["calendar_revision"] is None
        assert row["timezone"] == MARKETS[market][0]
    assert await test_session.scalar(select(func.count()).select_from(CalendarYearRevision)) == 0


@pytest.mark.asyncio
async def test_partial_published_overrides_defaults_and_draft_is_ignored(test_session):
    for revision, status, day, is_open in [
        (1, "published", date(2024, 2, 29), False),
        (2, "draft", date(2024, 3, 1), False),
    ]:
        row = CalendarYearRevision(
            market="TW",
            year=2024,
            revision=revision,
            status=status,
            timezone="Asia/Taipei",
            source_kind="manual",
            expected_days=366,
            actual_days=1,
        )
        test_session.add(row)
        await test_session.flush()
        test_session.add(
            CalendarRevisionDay(
                calendar_revision_id=row.id,
                trade_date=day,
                day_status="open" if is_open else "closed",
                is_open=is_open,
                source_kind="manual",
            )
        )
    await test_session.flush()
    rows = await effective_days(test_session, "TW", date(2024, 2, 28), date(2024, 3, 2))
    assert [row["is_open"] for row in rows] == [True, False, True, False]
    assert [row["calendar_revision"] for row in rows] == [None, 1, None, None]
    assert rows[1]["regular_close"] == "13:30:00"


@pytest.mark.asyncio
async def test_independent_configured_market_remains_queryable_and_editable(
    test_session, client, admin_headers
):
    from app.models.canonical import CalendarMarket
    from app.services.effective_calendar import ensure_market_for_edit
    from tests.test_calendar_management import _calendar_serve_headers

    market = CalendarMarket(
        market="JP", display_name="日本", timezone="Asia/Tokyo", weekend_days=[4, 5], active=True
    )
    test_session.add(market)
    await test_session.flush()
    assert await ensure_market_for_edit(test_session, "JP") is market
    markets = await client.get("/api/v1/admin/calendars/markets", headers=admin_headers)
    assert markets.status_code == 200
    assert {row["market"] for row in markets.json()} == {*MARKETS, "JP"}
    headers = await _calendar_serve_headers(test_session)
    year = await client.get("/api/v1/serve/calendar/years/JP/2024", headers=headers)
    assert year.status_code == 200, year.text
    assert year.json()["data"]["timezone"] == "Asia/Tokyo"
    assert year.json()["data"]["revision"] is None
    days = await client.get(
        "/api/v1/serve/calendar",
        headers=headers,
        params={"market": "JP", "start_date": "2024-02-29", "end_date": "2024-03-03"},
    )
    assert days.status_code == 200, days.text
    assert days.json()["data"] == [
        row
        for row in year.json()["data"]["days"]
        if "2024-02-29" <= row["trade_date"] <= "2024-03-03"
    ]
    assert [row["is_open"] for row in days.json()["data"]] == [True, False, False, True]
    assert await test_session.scalar(select(func.count()).select_from(CalendarYearRevision)) == 0


@pytest.mark.asyncio
async def test_builtin_market_uses_persisted_weekend_override_without_changing_exchange_metadata(
    test_session,
):
    from app.models.canonical import CalendarMarket

    test_session.add(
        CalendarMarket(
            market="TW",
            display_name="臺灣",
            timezone="UTC",
            weekend_days=[4, 5],
            default_session_open=time(1),
            default_session_close=time(2),
            active=True,
        )
    )
    await test_session.flush()

    rows = await effective_days(
        test_session,
        "TW",
        date(2024, 3, 1),
        date(2024, 3, 3),
    )

    assert [row["is_open"] for row in rows] == [False, False, True]
    assert {row["timezone"] for row in rows} == {"Asia/Taipei"}
    assert rows[-1]["session_open"] == "09:00:00"


@pytest.mark.asyncio
async def test_effective_calendar_does_not_mask_database_failure():
    from unittest.mock import AsyncMock

    db = AsyncMock()
    db.execute.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await effective_days(db, "TW", DAY, DAY)


FEEDS = [
    "us_equity_eod",
    "hk_equity_eod",
    "tw_equity_eod",
    "tw_etf_eod",
    "tw_equity_minute",
    "tw_etf_minute",
    "tw_futures_eod",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("key", FEEDS)
@pytest.mark.parametrize("skip", [False, True])
async def test_all_feeds_profiles_and_environments_freeze_weekend_decision(
    test_session, monkeypatch, environment, profile, key, skip
):
    dataset = await _dataset(test_session, key)
    body, release = await _baseline(test_session, key, count=3)
    await historical_admission(test_session, dataset, release, DAY, None, profile=profile)
    settings = get_settings()
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", environment)
    monkeypatch.setattr(settings, PROVIDER_CALENDAR_SWITCHES[body.provider], skip)
    test_session.add(FullMarketEnvironment(environment=environment, enabled=True))
    await test_session.commit()
    ready = await eligibility(test_session, body.provider, [dataset], profile=profile)
    assert ready["ready"] and not ready["blockers"]
    weekend = DAY + timedelta(days=(5 - DAY.weekday()) % 7)
    if body.provider == "taifex" and not skip:
        _, release = await _baseline(test_session, key, count=3, effective_date=weekend)
    request = DeliveryPlanCreateRequest(
        version=1,
        dataset_key=key,
        provider=body.provider,
        trade_date=weekend,
        release_id=release.release_id,
        execution_profile=profile,
    )
    result = await create_plan(test_session, request, allowed_datasets=[key])
    decision = await test_session.get(
        DailyExecutionDecision,
        {
            "dataset_key": key,
            "provider": body.provider,
            "trade_date": weekend,
            "execution_profile": profile,
        },
    )
    assert decision.snapshot["calendar_revision"] is None
    assert decision.snapshot["policy"]["skip_non_trading_days"] == skip
    assert (result.status == "skipped_calendar") == skip
    if not skip:
        expected = (2 if profile == "pilot" else 3) * (2 if key == "tw_futures_eod" else 1)
        assert result.summary.expected == expected
    frozen = dict(decision.snapshot)
    monkeypatch.setattr(settings, PROVIDER_CALENDAR_SWITCHES[body.provider], not skip)
    replay = await create_plan(test_session, request, allowed_datasets=[key])
    assert replay.model_dump() == result.model_dump()
    assert decision.snapshot == frozen
    assert await test_session.scalar(select(func.count()).select_from(DailyDeliveryPlan)) == (
        0 if skip else 1
    )
    assert await test_session.scalar(select(func.count()).select_from(DailyExecutionDecision)) == 1


@pytest.mark.asyncio
async def test_historical_skip_is_durable_and_claim_does_not_reevaluate(test_session, monkeypatch):
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    await test_session.execute(delete(CalendarRevisionDay))
    settings = get_settings()
    monkeypatch.setattr(settings, "SHIOAJI_SKIP_NON_TRADING_DAYS", True)
    request, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=date(2026, 8, 29),
        end_date=date(2026, 8, 31),
        request_key="frozen-skip-mixed",
        created_by="owner",
        today=today,
    )
    assert [item.status for item in request.items] == [
        "skipped_calendar",
        "skipped_calendar",
        "pending",
    ]
    monkeypatch.setattr(settings, "SHIOAJI_SKIP_NON_TRADING_DAYS", False)
    claimed = await claim_next_item(
        test_session,
        provider="shioaji",
        allowed_datasets=["tw_equity_minute"],
        now=datetime.combine(today, time(0), timezone.utc),
    )
    assert claimed.item_id == request.items[2].item_id
    assert claimed.calendar_snapshot["policy"]["skip_non_trading_days"] is True
    monkeypatch.setattr(settings, "SHIOAJI_SKIP_NON_TRADING_DAYS", True)
    skipped, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=date(2026, 8, 29),
        end_date=date(2026, 8, 30),
        request_key="frozen-skip-only",
        created_by="owner",
        today=today,
    )
    assert skipped.status == "completed"
    assert all(item.status == "skipped_calendar" and item.run_id is None for item in skipped.items)


@pytest.mark.asyncio
async def test_frozen_skip_and_plan_override_later_policy_in_monitor_projection(
    test_session, monkeypatch
):
    from app.models.registry import MissingDeliveryAlert
    from app.services.delivery_monitor import _scan_full_market_feed
    from app.services.effective_calendar import acquisition_dates

    dataset = await _dataset(test_session, "tw_equity_eod")
    body, release = await _baseline(test_session, "tw_equity_eod", count=1)
    await historical_admission(test_session, dataset, release, DAY, None)
    settings = get_settings()
    weekend = DAY + timedelta(days=(5 - DAY.weekday()) % 7)
    args = dict(
        market="TW",
        dataset_key=dataset.dataset_key,
        provider=body.provider,
        profile="full_market",
        start=weekend,
        end=weekend,
    )
    monkeypatch.setattr(settings, PROVIDER_CALENDAR_SWITCHES[body.provider], True)
    request = DeliveryPlanCreateRequest(
        version=1,
        dataset_key=dataset.dataset_key,
        provider=body.provider,
        trade_date=weekend,
        release_id=release.release_id,
    )
    assert (
        await create_plan(test_session, request, allowed_datasets=[dataset.dataset_key])
    ).status == "skipped_calendar"
    monkeypatch.setattr(settings, PROVIDER_CALENDAR_SWITCHES[body.provider], False)
    assert await acquisition_dates(test_session, **args) == []
    await _scan_full_market_feed(
        test_session,
        dataset=dataset,
        source=body.provider,
        schema_id="market_eod",
        schema_version=1,
        evaluated_at=datetime.combine(weekend, time(23), timezone.utc),
    )
    assert (
        await test_session.scalar(
            select(func.count())
            .select_from(MissingDeliveryAlert)
            .where(MissingDeliveryAlert.expected_data_date == weekend)
        )
        == 0
    )
    assert await test_session.scalar(select(func.count()).select_from(DailyDeliveryPlan)) == 0


@pytest.fixture(autouse=True)
def queue_clock(monkeypatch):
    from app.services import historical_backfill, provider_work

    monkeypatch.setattr(provider_work, "utc_now", lambda: historical_backfill.utc_now())
