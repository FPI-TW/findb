"""Stopped consumers remain cheap while immutable deliveries finish safely."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from test_full_market import _member
from test_full_market_recovery import _cycle_runtime, _frozen_plan, _release


def _setup(tmp_path, *, count=1, members=2):
    day = date(2026, 10, 1)
    release = _release(
        "frozen", "us_equity_eod", [{**_member(), "member_key": f"S{i}"} for i in range(members)]
    )
    runtime, source = _cycle_runtime(tmp_path, [release], [day])
    plans = []
    for offset in range(count):
        plan = _frozen_plan("us_equity_eod", day + timedelta(days=offset), release)
        source.plans[(plan["dataset_key"], plan["trade_date"])] = plan
        runtime.state.record_plan(plan, "twelve_data")
        plans.append(plan)
    runtime.can_acquire = lambda: False
    runtime._acquire = lambda *a: pytest.fail("stopped runtime acquired provider data")
    return runtime, source, plans


def _key(plan, member="S0"):
    return plan["parts"][0]["work_item_id"] + ":" + member
