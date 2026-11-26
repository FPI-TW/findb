"""A feed scheduled in another timezone, for the previous market date, resolves consistently."""

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.provider_catalog import ProviderCatalog
from app.services import feed_schedule, provider_metadata
from scripts.seed_data import earliest_feed

FRIDAY = date(2026, 11, 20)


def _catalog_with(feed_changes: dict) -> ProviderCatalog:
    data = provider_metadata.provider_catalog().model_dump(mode="json")
    template = next(f for f in data["feeds"] if f["dataset_key"] == "us_equity_eod")
    # Index rows of a New York date, fetched at 08:30 Taipei the next morning.
    feed = {
        **template,
        "dataset_key": "us_index_eod",
        "asset_class": "index",
        "schedule": {"timezone": "Asia/Taipei", "local_time": "08:30", "target_date_lag_days": 1},
        "completion_deadline": {
            "timezone": "Asia/Taipei",
            "local_time": "20:30",
            "trade_date_day_offset": 1,
        },
        "calendar_required": True,
        **feed_changes,
    }
    data["feeds"].append(feed)
    return ProviderCatalog.model_validate(data, strict=False)


@pytest.fixture
def lagged(monkeypatch):
    catalog = _catalog_with({})
    monkeypatch.setattr(provider_metadata, "provider_catalog", lambda: catalog)
    return next(f for f in catalog.feeds if f.dataset_key == "us_index_eod")


def test_catalog_accepts_an_index_feed_due_twelve_hours_after_its_lagged_start(lagged):
    assert lagged.asset_class == "index"
    assert lagged.calendar_market == "US"
    with pytest.raises(ValidationError, match="12 hours after the scheduled start"):
        # The deadline follows the schedule's own timezone and lag, not New York's.
        _catalog_with(
            {
                "completion_deadline": {
                    "timezone": "America/New_York",
                    "local_time": "20:30",
                    "trade_date_day_offset": 0,
                }
            }
        )


def test_new_york_date_is_ready_and_due_on_the_taipei_morning_after(lagged):
    ready = feed_schedule.data_ready_at(
        "twelve_data",
        "us_index_eod",
        FRIDAY,
        {"regular_close": "16:00:00", "timezone": "America/New_York"},
    )
    # Saturday 08:30 Taipei, later than Friday's 16:00 New York close (21:00 UTC).
    assert ready.astimezone(timezone.utc) == datetime(2026, 11, 21, 0, 30, tzinfo=timezone.utc)
    deadline = feed_schedule.completion_deadline_at("us_index_eod", FRIDAY)
    assert deadline == datetime(2026, 11, 21, 12, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        # Saturday 08:29 Taipei is Friday 19:29 New York: Friday is not yet due.
        ("2026-11-21T00:29:59+00:00", "2026-11-19"),
        ("2026-11-21T00:30:00+00:00", "2026-11-20"),
        # Monday 08:30 Taipei targets Sunday, which the US calendar does not plan.
        ("2026-11-23T00:30:00+00:00", "2026-11-22"),
        # Taipei has no daylight saving: the same UTC trigger in July.
        ("2026-07-07T00:30:00+00:00", "2026-07-06"),
    ],
)
def test_latest_due_date_counts_back_from_the_taipei_date(lagged, clock, expected):
    due = feed_schedule.latest_due_date("us_index_eod", datetime.fromisoformat(clock))
    assert due.isoformat() == expected


def test_next_start_and_published_snapshot_carry_the_schedule_timezone(lagged):
    view = feed_schedule.feed_schedule_view(
        "twelve_data", "us_index_eod", datetime(2026, 11, 21, 0, 30, tzinfo=timezone.utc)
    )
    assert view is not None
    assert (view.source_timezone, view.scheduled_time, view.target_date_lag_days) == (
        "Asia/Taipei",
        "08:30",
        1,
    )
    assert view.next_scheduled_at == datetime(2026, 11, 22, 0, 30, tzinfo=timezone.utc)
    assert view.asset_class == "index"


def test_control_clock_is_the_earliest_start_across_timezones(lagged):
    catalog = provider_metadata.provider_catalog()
    hong_kong = _catalog_with(
        {
            "dataset_key": "hk_index_eod",
            "market": "HK",
            "calendar_market": "HK",
            "schedule": {
                "timezone": "Asia/Hong_Kong",
                "local_time": "20:00",
                "target_date_lag_days": 0,
            },
            "completion_deadline": {
                "timezone": "Asia/Hong_Kong",
                "local_time": "08:00",
                "trade_date_day_offset": 1,
            },
        }
    ).feeds[-1]
    us_index = next(f for f in catalog.feeds if f.dataset_key == "us_index_eod")
    us_equity = next(f for f in catalog.feeds if f.dataset_key == "us_equity_eod")
    # 08:30 Taipei is 00:30 UTC and 20:00 Hong Kong 12:00 UTC; 17:00 New York (22:00 UTC)
    # starts after 20:00 Hong Kong although its wall clock reads earlier.
    assert earliest_feed([hong_kong, us_index]) is us_index
    assert earliest_feed([us_equity, hong_kong]) is hong_kong
    finlab = [f for f in catalog.feeds if f.provider == "finlab" and f.schedule is not None]
    assert earliest_feed(finlab).schedule.local_time == "19:30"


def test_seed_delivery_expectations_use_catalog_slots():
    from scripts.seed_data import DATASETS

    feeds = {feed.dataset_key: feed for feed in provider_metadata.acquisition_feeds()}
    checked = 0
    for definition in DATASETS:
        feed = feeds.get(definition["dataset_key"])
        if feed is None:
            continue
        schedule = definition["config"]["delivery_expectation"]["schedule"]
        assert schedule["slot_id"] == feed.delivery.slot_id, definition["dataset_key"]
        checked += 1
    assert checked == len(feeds)
