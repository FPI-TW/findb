from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from findb_fetcher.schedule import ScheduleError, load_schedule_manifest

V2_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "daily_scheduler.v2.json"
V2_CALENDAR_PATH = V2_CONFIG_PATH.parent / "calendars" / "us_equity_2026_2028.v1.json"
V3_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "configs" / "daily_scheduler.production.v3.json"
)


def _write_v2_manifest(tmp_path: Path, value: object) -> Path:
    calendar_dir = tmp_path / "calendars"
    calendar_dir.mkdir(exist_ok=True)
    (calendar_dir / V2_CALENDAR_PATH.name).write_text(V2_CALENDAR_PATH.read_text())
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value))
    return path


def test_repository_schedule_is_strict_and_bounded() -> None:
    manifest = load_schedule_manifest(V2_CONFIG_PATH)
    schedule = manifest.feeds[0]

    assert manifest.schedule_version == 2
    assert schedule.schedule_id == "twelve_data_western_markets_window_us_equity_eod"
    assert schedule.universe_file.name == "twelve_data_us_common_stocks.v1.json"
    assert schedule.scheduled_local_time.isoformat() == "08:15:00"
    assert schedule.outputsize == 20
    assert schedule.max_attempts == 5
    assert schedule.lease_seconds > schedule.wait_timeout_seconds


def test_production_schedule_is_target_bound_and_uses_reviewed_universes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEPLOYMENT_TARGET", "production")
    manifest = load_schedule_manifest(V3_CONFIG_PATH)

    assert manifest.schedule_version == 3
    assert manifest.deployment_target == "production"
    assert [feed.universe_file.name for feed in manifest.feeds] == [
        "twelve_data_nasdaq_100_2026_09_14.v2.json",
        "finlab_tw50_2026_09_21.v2.json",
    ]
    tw_feed = manifest.feeds[1]
    assert tw_feed.target_date_policy == "latest_trade_date"
    assert tw_feed.calendar_id == "tw_equity_2025_2026"
    assert tw_feed.target_date(datetime(2025, 1, 24, 6, 30, tzinfo=timezone.utc)) == date(
        2025, 1, 22
    )
    assert tw_feed.target_date(datetime(2026, 2, 20, 6, 30, tzinfo=timezone.utc)) == date(
        2026, 2, 11
    )
    with pytest.raises(ScheduleError, match="outside the governed market calendar"):
        tw_feed.target_date(datetime(2027, 1, 4, 6, 30, tzinfo=timezone.utc))
    with pytest.raises(ScheduleError, match="production target"):
        load_schedule_manifest(V2_CONFIG_PATH)


def test_staging_target_rejects_production_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPLOYMENT_TARGET", "staging")
    with pytest.raises(ScheduleError, match="does not match"):
        load_schedule_manifest(V3_CONFIG_PATH)


def test_v1_schedule_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "legacy-schedule.json"
    path.write_text(
        json.dumps(
            {
                "schedule_version": 1,
                "schedule_id": "twelve_data_us_common_stocks_daily_v1",
                "universe_file": "twelve_data_us_common_stocks.v1.json",
            }
        )
    )
    with pytest.raises(ScheduleError, match="v2 schedule keys"):
        load_schedule_manifest(path)


def test_retry_delay_is_exponential_and_bounded() -> None:
    schedule = load_schedule_manifest(V2_CONFIG_PATH).feeds[0]

    assert [schedule.retry_delay_seconds(attempt) for attempt in range(1, 8)] == [
        60,
        120,
        240,
        480,
        960,
        1920,
        3600,
    ]


def test_v2_manifest_has_only_active_daily_feeds_and_date_boundaries() -> None:
    manifest = load_schedule_manifest(V2_CONFIG_PATH)
    assert manifest.schedule_version == 2
    assert [feed.slot_id for feed in manifest.feeds] == [
        "western_markets_window",
        "taiwan_market_window",
    ]
    assert [feed.scheduled_local_time.isoformat() for feed in manifest.feeds] == [
        "08:15:00",
        "14:30:00",
    ]
    assert [feed.target_date_lag_days for feed in manifest.feeds] == [1, 0]
    assert (
        manifest.feeds[1].target_date(datetime(2026, 7, 30, 6, 29, tzinfo=timezone.utc)).isoformat()
        == "2026-07-29"
    )
    assert (
        manifest.feeds[1].target_date(datetime(2026, 7, 30, 6, 30, tzinfo=timezone.utc)).isoformat()
        == "2026-07-30"
    )


def test_western_slot_targets_the_previous_us_weekday() -> None:
    us_feed = load_schedule_manifest(V2_CONFIG_PATH).feeds[0]

    assert (
        us_feed.scheduled_date(datetime(2026, 7, 31, 0, 14, tzinfo=timezone.utc))
        == datetime(2026, 7, 30, tzinfo=timezone.utc).date()
    )
    assert (
        us_feed.target_date(datetime(2026, 7, 31, 0, 14, tzinfo=timezone.utc)).isoformat()
        == "2026-07-29"
    )
    assert (
        us_feed.target_date(datetime(2026, 7, 31, 0, 15, tzinfo=timezone.utc)).isoformat()
        == "2026-07-30"
    )
    assert (
        us_feed.target_date(datetime(2026, 7, 27, 0, 15, tzinfo=timezone.utc)).isoformat()
        == "2026-07-24"
    )


def test_us_trade_date_policy_uses_governed_holidays_and_fails_closed() -> None:
    us_feed = load_schedule_manifest(V2_CONFIG_PATH).feeds[0]

    assert (
        us_feed.target_date(datetime(2026, 7, 4, 0, 15, tzinfo=timezone.utc)).isoformat()
        == "2026-07-02"
    )
    assert (
        us_feed.target_date(datetime(2028, 7, 6, 0, 15, tzinfo=timezone.utc)).isoformat()
        == "2028-07-05"
    )
    assert (
        us_feed.target_date(datetime(2028, 7, 5, 0, 15, tzinfo=timezone.utc)).isoformat()
        == "2028-07-03"
    )
    assert (
        us_feed.target_date(datetime(2027, 11, 27, 0, 15, tzinfo=timezone.utc)).isoformat()
        == "2027-11-26"
    )
    with pytest.raises(ScheduleError, match="governed market calendar"):
        us_feed.target_date(datetime(2029, 1, 5, 0, 15, tzinfo=timezone.utc))
    with pytest.raises(ScheduleError, match="governed market calendar"):
        us_feed.target_date(datetime(2026, 1, 2, 0, 15, tzinfo=timezone.utc))


def test_v2_manifest_allows_multiple_unique_feeds_in_one_slot(
    tmp_path: Path,
) -> None:
    value = json.loads(V2_CONFIG_PATH.read_text())
    additional = deepcopy(value["feeds"][0])
    additional["dataset_key"] = "us_index_eod"
    additional["enabled"] = False
    value["feeds"].append(additional)
    path = _write_v2_manifest(tmp_path, value)

    manifest = load_schedule_manifest(path)

    assert len(manifest.feeds) == 3
    assert [feed.slot_id for feed in manifest.feeds].count("western_markets_window") == 2

    value["feeds"].append(deepcopy(additional))
    path.write_text(json.dumps(value))
    with pytest.raises(ScheduleError, match="identity"):
        load_schedule_manifest(path)


def test_v2_scheduled_time_is_data_not_slot_identity(tmp_path: Path) -> None:
    value = json.loads(V2_CONFIG_PATH.read_text())
    original_path = _write_v2_manifest(tmp_path, value)
    original = load_schedule_manifest(original_path).feeds[1]

    value["feeds"][1]["scheduled_time"] = "15:00"
    changed_path = tmp_path / "changed.json"
    changed_path.write_text(json.dumps(value))
    changed = load_schedule_manifest(changed_path).feeds[1]

    assert changed.slot_id == original.slot_id == "taiwan_market_window"
    assert changed.schedule_id == original.schedule_id
    assert changed.scheduled_local_time.isoformat() == "15:00:00"
    assert changed.scheduled_date(datetime(2026, 7, 30, 7, 0, tzinfo=timezone.utc)) == date(
        2026, 7, 30
    )


def test_v2_manifest_rejects_removed_schedule_ownership_key(tmp_path: Path) -> None:
    value = json.loads(V2_CONFIG_PATH.read_text())
    value["feeds"][0]["legacy_schedule_id"] = "old_schedule"
    path = _write_v2_manifest(tmp_path, value)

    with pytest.raises(ScheduleError, match="keys must be exactly"):
        load_schedule_manifest(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schedule_version", 1, "schedule_version"),
        ("timezone", "UTC", "timezone"),
        ("universe_file", "../secrets.json", "universe_file"),
        ("outputsize", 5001, "outputsize"),
        ("max_attempts", 11, "max_attempts"),
        ("retry_base_seconds", 3601, "retry_base_seconds"),
        ("lease_seconds", 1800, "lease"),
    ],
)
def test_invalid_schedule_values_are_rejected(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    config = json.loads(V2_CONFIG_PATH.read_text())
    if field in {"schedule_version", "timezone"}:
        config[field] = value
    else:
        config["feeds"][0][field] = value
    path = _write_v2_manifest(tmp_path, config)

    with pytest.raises(ScheduleError, match=message):
        load_schedule_manifest(path)


def test_unknown_and_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    config = json.loads(V2_CONFIG_PATH.read_text())
    config["secret"] = "not-allowed"
    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps(config))
    with pytest.raises(ScheduleError, match="keys must be exactly"):
        load_schedule_manifest(unknown)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schedule_version":2,"schedule_version":2}')
    with pytest.raises(ScheduleError, match="unique keys"):
        load_schedule_manifest(duplicate)


@pytest.mark.parametrize(
    ("calendar_file", "message"),
    [
        ("/tmp/calendar.json", "calendar_file"),
        ("calendars/../calendar.json", "calendar_file"),
        ("calendars/calendar.txt", "calendar_file"),
    ],
)
def test_v2_calendar_reference_rejects_unsafe_paths(
    tmp_path: Path,
    calendar_file: str,
    message: str,
) -> None:
    value = json.loads(V2_CONFIG_PATH.read_text())
    value["feeds"][0]["calendar_file"] = calendar_file
    path = _write_v2_manifest(tmp_path, value)

    with pytest.raises(ScheduleError, match=message):
        load_schedule_manifest(path)


def test_v2_calendar_reference_rejects_symlink_escape(tmp_path: Path) -> None:
    value = json.loads(V2_CONFIG_PATH.read_text())
    path = _write_v2_manifest(tmp_path, value)
    calendar_path = path.parent / "calendars" / V2_CALENDAR_PATH.name
    escaped_calendar = tmp_path / "escaped-calendar.json"
    escaped_calendar.write_text(V2_CALENDAR_PATH.read_text())
    try:
        calendar_path.unlink()
        calendar_path.symlink_to(escaped_calendar)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable on this filesystem: {exc}")

    with pytest.raises(ScheduleError, match="governed configs/calendars"):
        load_schedule_manifest(path)


def test_v2_calendar_rejects_invalid_or_mismatched_contents(tmp_path: Path) -> None:
    value = json.loads(V2_CONFIG_PATH.read_text())
    path = _write_v2_manifest(tmp_path, value)
    calendar_path = path.parent / "calendars" / V2_CALENDAR_PATH.name
    calendar = json.loads(calendar_path.read_text())

    calendar["unknown"] = True
    calendar_path.write_text(json.dumps(calendar))
    with pytest.raises(ScheduleError, match="calendar keys"):
        load_schedule_manifest(path)

    calendar.pop("unknown")
    calendar["market"] = "TW"
    calendar_path.write_text(json.dumps(calendar))
    with pytest.raises(ScheduleError, match="market must match"):
        load_schedule_manifest(path)

    calendar["market"] = "US"
    calendar["timezone"] = "UTC"
    calendar_path.write_text(json.dumps(calendar))
    with pytest.raises(ScheduleError, match="timezone must match"):
        load_schedule_manifest(path)

    calendar["timezone"] = "Asia/Taipei"
    calendar["coverage_start_date"] = "2026-12-31"
    calendar["coverage_end_date"] = "2026-01-01"
    calendar_path.write_text(json.dumps(calendar))
    with pytest.raises(ScheduleError, match="date range"):
        load_schedule_manifest(path)

    calendar["coverage_start_date"] = "2026-01-01"
    calendar["coverage_end_date"] = "2026-12-31"
    calendar["non_trading_dates"].append("2026-07-03")
    calendar_path.write_text(json.dumps(calendar))
    with pytest.raises(ScheduleError, match="unique ISO dates"):
        load_schedule_manifest(path)

    calendar["non_trading_dates"].pop()
    calendar["non_trading_dates"].append("2029-01-01")
    calendar_path.write_text(json.dumps(calendar))
    with pytest.raises(ScheduleError, match="date range"):
        load_schedule_manifest(path)


def test_v2_calendar_rejects_duplicate_keys_and_missing_enabled_calendar(tmp_path: Path) -> None:
    value = json.loads(V2_CONFIG_PATH.read_text())
    path = _write_v2_manifest(tmp_path, value)
    calendar_path = path.parent / "calendars" / V2_CALENDAR_PATH.name
    calendar_path.write_text('{"calendar_version":1,"calendar_version":1}')
    with pytest.raises(ScheduleError, match="unique keys"):
        load_schedule_manifest(path)

    path = _write_v2_manifest(tmp_path, value)
    value["feeds"][0]["calendar_file"] = None
    path.write_text(json.dumps(value))
    with pytest.raises(ScheduleError, match="requires a governed calendar"):
        load_schedule_manifest(path)
