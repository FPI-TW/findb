"""A daily round tells its adapter it starts, with the member context, before any member."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from findb_fetcher.full_market_state import FullMarketState
from test_full_market import _runtime


def _plan(day: str = "2026-10-01") -> dict:
    return {
        "plan_id": f"plan-{day}",
        "dataset_key": "us_equity_eod",
        "trade_date": day,
        "parts": [{"work_item_id": "fp1:123", "member_keys": ["AAPL"]}],
    }


@pytest.mark.parametrize("drain_only", [False, True])
def test_a_round_starts_before_its_members_and_never_while_draining(tmp_path, drain_only) -> None:
    runtime, _, _ = _runtime(tmp_path)
    events: list[object] = []
    runtime.adapter.round_started = lambda context: events.append(  # type: ignore[method-assign]
        ("started", context.dataset_key, context.trade_date, context.earlier_plan_date())
    )
    runtime._deliver_members = lambda *args, **kwargs: events.append("members") or True  # type: ignore[method-assign]
    runtime.state.record_plan(_plan("2026-09-30"), "twelve_data")

    runtime._deliver_plan(_plan(), {"members": []}, runtime.feeds[0], drain_only=drain_only)

    started = ("started", "us_equity_eod", date(2026, 10, 1), date(2026, 9, 30))
    assert events == (["members"] if drain_only else [started, "members"])
    runtime.close()


def test_an_adapter_failure_at_round_start_never_decides_the_round(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path)

    def broken(context):
        raise RuntimeError("adapter check failed")

    runtime.adapter.round_started = broken  # type: ignore[method-assign]
    runtime._deliver_members = lambda *args, **kwargs: True  # type: ignore[method-assign]

    assert runtime._deliver_plan(_plan(), {"members": []}, runtime.feeds[0]) is True
    runtime.close()


def test_the_latest_earlier_plan_date_is_the_dataset_s_own(tmp_path: Path) -> None:
    state = FullMarketState(tmp_path / "state.sqlite3")
    assert state.latest_plan_date_before("twelve_data", "us_equity_eod", date(2026, 10, 1)) is None
    for day in ("2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02"):
        state.record_plan(_plan(day), "twelve_data")

    assert state.latest_plan_date_before("twelve_data", "us_equity_eod", date(2026, 10, 1)) == date(
        2026, 9, 30
    )
    assert state.latest_plan_date_before("finlab", "us_equity_eod", date(2026, 10, 1)) is None
