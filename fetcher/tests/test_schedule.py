from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from findb_fetcher.schedule import ScheduleError, load_schedule_config, load_schedule_manifest

CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "configs" / "twelve_data_us_common_stocks_daily.v1.json"
)
V2_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "daily_scheduler.v2.json"
V2_CALENDAR_PATH = V2_CONFIG_PATH.parent / "calendars" / "us_equity_2026_2028.v1.json"


def _write_v2_manifest(tmp_path: Path, value: object) -> Path:
    calendar_dir = tmp_path / "calendars"
    calendar_dir.mkdir(exist_ok=True)
    (calendar_dir / V2_CALENDAR_PATH.name).write_text(V2_CALENDAR_PATH.read_text())
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value))
    return path


def test_repository_schedule_is_strict_and_bounded() -> None:
    schedule = load_schedule_config(CONFIG_PATH)

    assert schedule.schedule_version == 1
    assert schedule.schedule_id == "twelve_data_us_common_stocks_daily_v1"
    assert schedule.universe_file.name == "twelve_data_us_common_stocks.v1.json"
    assert schedule.hour_utc == 22
    assert schedule.minute_utc == 0
    assert schedule.outputsize == 20
    assert schedule.max_attempts == 5
    assert schedule.lease_seconds > schedule.wait_timeout_seconds


def test_latest_due_date_uses_utc_schedule_boundary() -> None:
    schedule = load_schedule_config(CONFIG_PATH)

    assert schedule.latest_due_date(
        datetime(2026, 7, 25, 21, 59, tzinfo=timezone.utc)
    ).isoformat() == ("2026-07-24")
    assert schedule.latest_due_date(
        datetime(2026, 7, 25, 22, 0, tzinfo=timezone.utc)
    ).isoformat() == ("2026-07-24")
    assert schedule.latest_due_date(
        datetime(2026, 7, 27, 21, 59, tzinfo=timezone.utc)
    ).isoformat() == ("2026-07-24")
    assert schedule.latest_due_date(
        datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)
    ).isoformat() == ("2026-07-27")

    with pytest.raises(ScheduleError, match="timezone-aware"):
        schedule.latest_due_date(datetime(2026, 7, 25, 22, 0))


def test_retry_delay_is_exponential_and_bounded() -> None:
    schedule = load_schedule_config(CONFIG_PATH)

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
        "06:30:00",
        "14:30:00",
    ]
    assert [feed.target_date_lag_days for feed in manifest.feeds] == [1, 0]
    assert [feed.legacy_schedule_id for feed in manifest.feeds] == [
        "twelve_data_us_common_stocks_daily_v1",
        None,
    ]
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
        us_feed.scheduled_date(datetime(2026, 7, 30, 22, 29, tzinfo=timezone.utc))
        == datetime(2026, 7, 30, tzinfo=timezone.utc).date()
    )
    assert (
        us_feed.target_date(datetime(2026, 7, 30, 22, 29, tzinfo=timezone.utc)).isoformat()
        == "2026-07-29"
    )
    assert (
        us_feed.target_date(datetime(2026, 7, 30, 22, 30, tzinfo=timezone.utc)).isoformat()
        == "2026-07-30"
    )
    assert (
        us_feed.target_date(datetime(2026, 7, 26, 22, 30, tzinfo=timezone.utc)).isoformat()
        == "2026-07-24"
    )


def test_us_trade_date_policy_uses_governed_holidays_and_fails_closed() -> None:
    us_feed = load_schedule_manifest(V2_CONFIG_PATH).feeds[0]

    assert (
        us_feed.target_date(datetime(2026, 7, 3, 22, 30, tzinfo=timezone.utc)).isoformat()
        == "2026-07-02"
    )
    assert (
        us_feed.target_date(datetime(2028, 7, 5, 22, 30, tzinfo=timezone.utc)).isoformat()
        == "2028-07-05"
    )
    assert (
        us_feed.target_date(datetime(2028, 7, 4, 22, 30, tzinfo=timezone.utc)).isoformat()
        == "2028-07-03"
    )
    assert (
        us_feed.target_date(datetime(2027, 11, 26, 22, 30, tzinfo=timezone.utc)).isoformat()
        == "2027-11-26"
    )
    with pytest.raises(ScheduleError, match="governed market calendar"):
        us_feed.target_date(datetime(2029, 1, 4, 22, 30, tzinfo=timezone.utc))
    with pytest.raises(ScheduleError, match="governed market calendar"):
        us_feed.target_date(datetime(2026, 1, 1, 22, 30, tzinfo=timezone.utc))


def test_v2_manifest_allows_multiple_unique_feeds_in_one_slot(
    tmp_path: Path,
) -> None:
    value = json.loads(V2_CONFIG_PATH.read_text())
    additional = deepcopy(value["feeds"][0])
    additional["dataset_key"] = "us_index_eod"
    additional["legacy_schedule_id"] = None
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


@pytest.mark.parametrize(
    ("legacy_schedule_id", "message"),
    [
        ("", "legacy_schedule_id"),
        ("Not-Stable", "legacy_schedule_id"),
        (123, "legacy_schedule_id"),
    ],
)
def test_v2_legacy_schedule_id_is_strictly_null_or_stable(
    tmp_path: Path,
    legacy_schedule_id: object,
    message: str,
) -> None:
    value = json.loads(V2_CONFIG_PATH.read_text())
    value["feeds"][0]["legacy_schedule_id"] = legacy_schedule_id
    path = _write_v2_manifest(tmp_path, value)

    with pytest.raises(ScheduleError, match=message):
        load_schedule_manifest(path)


def test_v2_legacy_schedule_id_cannot_be_claimed_by_two_feeds(tmp_path: Path) -> None:
    value = json.loads(V2_CONFIG_PATH.read_text())
    value["feeds"][1]["legacy_schedule_id"] = "twelve_data_us_common_stocks_daily_v1"
    path = _write_v2_manifest(tmp_path, value)

    with pytest.raises(ScheduleError, match="legacy_schedule_id"):
        load_schedule_manifest(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schedule_version", 2, "schedule_version"),
        ("schedule_id", "Not-Stable", "schedule_id"),
        ("universe_file", "../secrets.json", "universe_file"),
        ("hour_utc", 24, "hour_utc"),
        ("outputsize", 5001, "outputsize"),
        ("max_attempts", 11, "max_attempts"),
        ("retry_base_seconds", 3601, "retry_base_seconds"),
        ("lease_seconds", 1800, "lease_seconds"),
    ],
)
def test_invalid_schedule_values_are_rejected(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    config = json.loads(CONFIG_PATH.read_text())
    config[field] = value
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(config))

    with pytest.raises(ScheduleError, match=message):
        load_schedule_config(path)


def test_unknown_and_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    config = json.loads(CONFIG_PATH.read_text())
    config["secret"] = "not-allowed"
    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps(config))
    with pytest.raises(ScheduleError, match="keys must be exactly"):
        load_schedule_config(unknown)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schedule_version":1,"schedule_version":1}')
    with pytest.raises(ScheduleError, match="unique keys"):
        load_schedule_config(duplicate)


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
