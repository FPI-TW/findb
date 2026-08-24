from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from findb_fetcher.market_calendar import CalendarDay, MarketCalendarError
from findb_fetcher.schedule import load_schedule_manifest
from findb_fetcher.twelve_data_scheduler import SchedulerService

CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "daily_scheduler.v2.json"


class EmptyState:
    def __init__(self) -> None:
        self.target_data_date: date | None = None

    def enqueue_due(
        self,
        _schedule: Any,
        _universe: Any,
        _scheduled_date: date,
        *,
        target_data_date: date,
        now: datetime,
    ) -> int:
        del now
        self.target_data_date = target_data_date
        return 0

    def claim_due(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    def status_counts(self, *_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {}


class Calendar:
    def __init__(self, status: str, *, failure: Exception | None = None) -> None:
        self.status = status
        self.failure = failure
        self.calls: list[tuple[str, date]] = []

    def get_day(self, market: str, value: date) -> tuple[CalendarDay, int]:
        self.calls.append((market, value))
        if self.failure:
            raise self.failure
        return (
            CalendarDay(
                trade_date=value,
                day_status=self.status,
                is_open=self.status == "open",
            ),
            7,
        )


def _service(
    status: str, *, slot_id: str = "taiwan_market_window"
) -> tuple[SchedulerService, EmptyState, Calendar]:
    current = next(
        feed for feed in load_schedule_manifest(CONFIG_PATH).feeds if feed.slot_id == slot_id
    )
    schedule = replace(
        current,
        schedule_version=2,
        slot_id=slot_id,
        market="TW" if slot_id == "taiwan_market_window" else "US",
        timezone_name="Asia/Taipei",
        scheduled_local_time=time(14, 30) if slot_id == "taiwan_market_window" else time(6, 30),
        target_date_lag_days=0 if slot_id == "taiwan_market_window" else 1,
        target_date_policy="latest_trade_date",
    )
    state = EmptyState()
    calendar = Calendar(status)
    universe = SimpleNamespace(
        limits=SimpleNamespace(max_records_per_symbol=5000),
        symbols=(),
        credit_cost_per_symbol=1,
    )
    return (
        SchedulerService(
            schedule=schedule,
            universe=universe,  # type: ignore[arg-type]
            state=state,  # type: ignore[arg-type]
            executor=object(),  # type: ignore[arg-type]
            calendar=calendar,
        ),
        state,
        calendar,
    )


@pytest.mark.parametrize("status", ["closed", "settlement_only"])
def test_non_trading_calendar_day_skips_without_enqueuing(status: str) -> None:
    service, state, _calendar = _service(status)

    result = service.run_once(now=datetime(2026, 2, 12, 7, 0, tzinfo=timezone.utc))

    assert result.status == "skipped"
    assert result.skip_reason == f"calendar_{status}"
    assert result.calendar_revision == 7
    assert result.enqueued == result.claimed == 0
    assert state.target_data_date is None


def test_open_day_enqueues_exact_calendar_date() -> None:
    service, state, calendar = _service("open")

    result = service.run_once(now=datetime(2026, 2, 12, 7, 0, tzinfo=timezone.utc))

    assert result.status == "completed"
    assert result.calendar_revision == 7
    assert calendar.calls == [("TW", date(2026, 2, 12))]
    assert state.target_data_date == date(2026, 2, 12)


def test_us_slot_checks_previous_market_date() -> None:
    service, state, calendar = _service("open", slot_id="western_markets_window")

    service.run_once(now=datetime(2026, 2, 12, 0, 0, tzinfo=timezone.utc))

    assert calendar.calls == [("US", date(2026, 2, 11))]
    assert state.target_data_date == date(2026, 2, 11)


def test_calendar_failure_propagates_and_never_enqueues() -> None:
    service, state, calendar = _service("open")
    calendar.failure = MarketCalendarError("unavailable")

    with pytest.raises(MarketCalendarError, match="unavailable"):
        service.run_once(now=datetime(2026, 2, 12, 7, 0, tzinfo=timezone.utc))
    assert state.target_data_date is None
