from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models.canonical import CalendarRevisionDay, CalendarYearRevision
from app.models.registry import DatasetRegistry, SchedulerControl, SchedulerDataset
from app.services import historical_backfill as history
from app.utils import uuid7
from tests.owned_history import claim_next_item


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    "provider,dataset,market,zone,normal_close",
    [
        ("shioaji", "tw_equity_minute", "TW", "Asia/Taipei", time(13, 30)),
        ("shioaji", "tw_etf_minute", "TW", "Asia/Taipei", time(13, 30)),
        ("finlab", "tw_equity_eod", "TW", "Asia/Taipei", time(13, 30)),
        ("twelve_data", "us_equity_eod", "US", "America/New_York", time(16)),
    ],
)
@pytest.mark.parametrize("calendar_kind", ["default", "weekend", "early_close"])
async def test_historical_acquisition_uses_frozen_close(
    test_session,
    monkeypatch,
    environment,
    provider,
    dataset,
    market,
    zone,
    normal_close,
    calendar_kind,
):
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    day = date(2026, 9, 5) if calendar_kind == "weekend" else date(2026, 9, 1)
    close = time(11) if calendar_kind == "early_close" else normal_close
    closed_at = datetime.combine(day, close, ZoneInfo(zone)).astimezone(timezone.utc)
    now = closed_at - timedelta(hours=8)
    monkeypatch.setattr(history, "utc_now", lambda: now)
    test_session.add(
        DatasetRegistry(dataset_key=dataset, name=dataset, asset_class="equity", market=market)
    )
    test_session.add(
        SchedulerControl(
            scheduler_key="close-test",
            provider=provider,
            timezone=zone,
            slot_id="western_markets_window" if market == "US" else "taiwan_market_window",
        )
    )
    await test_session.flush()
    test_session.add(SchedulerDataset(scheduler_key="close-test", dataset_key=dataset))
    published = None
    if calendar_kind == "early_close":
        revision = CalendarYearRevision(
            id=uuid7(),
            market=market,
            year=day.year,
            revision=1,
            status="published",
            timezone=zone,
            expected_days=365,
            actual_days=1,
            source_kind="test",
        )
        test_session.add(revision)
        await test_session.flush()
        published = CalendarRevisionDay(
            id=uuid7(),
            calendar_revision_id=revision.id,
            trade_date=day,
            is_open=True,
            day_status="open",
            session_close=close,
            source_kind="test",
        )
        test_session.add(published)
    await test_session.flush()
    request, _ = await history.create_request(
        test_session,
        provider=provider,
        dataset_key=dataset,
        start_date=day,
        end_date=day,
        request_key="frozen-close",
        created_by="owner",
        today=day,
    )
    item = request.items[0]
    snapshot = dict(item.calendar_snapshot)
    assert snapshot["regular_close"] == close.isoformat()
    assert snapshot["is_open"] is (calendar_kind != "weekend")
    assert (
        await claim_next_item(test_session, provider=provider, allowed_datasets=[dataset], now=now)
        is None
    )
    assert item.status == "pending" and item.lease_token is None
    assert request.status == "queued"

    # An existing lease never authorizes a new provider call before the frozen close.
    request.status, item.status = "running", "running"
    item.lease_token, item.lease_expires_at = uuid7(), closed_at + timedelta(hours=1)
    allowed, *_ = await history.authorize_item(
        test_session,
        item.item_id,
        provider=provider,
        allowed_datasets=[dataset],
        lease_token=item.lease_token,
    )
    assert allowed is False
    request.status, item.status = "queued", "pending"
    item.lease_token, item.lease_expires_at = None, None
    if published is not None:
        published.session_close = normal_close
    await test_session.flush()
    now = closed_at - timedelta(microseconds=1)
    assert (
        await claim_next_item(test_session, provider=provider, allowed_datasets=[dataset], now=now)
        is None
    )
    now = closed_at
    claimed = await claim_next_item(
        test_session, provider=provider, allowed_datasets=[dataset], now=now
    )
    assert claimed is item and item.lease_token is not None
    allowed, *_ = await history.authorize_item(
        test_session,
        item.item_id,
        provider=provider,
        allowed_datasets=[dataset],
        lease_token=item.lease_token,
    )
    assert allowed is True
    assert item.calendar_snapshot == snapshot


@pytest.fixture(autouse=True)
def queue_clock(monkeypatch):
    from app.services import historical_backfill, provider_work

    monkeypatch.setattr(provider_work, "utc_now", lambda: historical_backfill.utc_now())
