from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from findb_fetcher.schedule import ScheduleError, load_schedule_config

CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "configs" / "twelve_data_us_common_stocks_daily.v1.json"
)


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
