"""Whole-work settlement distinguishes provider publication waits from failures."""

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from findb_fetcher.provider_wait import (
    CalendarDecisionRepairRequiredError,
    ProviderPublicationPendingError,
)
from test_full_market import _runtime


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ProviderPublicationPendingError("finlab_target_not_ready", retry_after_seconds=731),
            {
                "outcome": "retry",
                "failure_code": "DAILY_PROVIDER_NOT_READY",
                "retry_delay_seconds": 731,
            },
        ),
        (
            CalendarDecisionRepairRequiredError("published calendar is now closed"),
            {
                "outcome": "failed",
                "failure_code": "CALENDAR_DECISION_REPAIR_REQUIRED",
            },
        ),
    ],
)
def test_publication_wait_settles_one_round_with_explicit_policy(
    tmp_path, monkeypatch, error, expected
):
    from findb_fetcher import provider_work

    runtime, source, _ = _runtime(tmp_path, provider="finlab", execution_profile="pilot")
    feed = runtime.feeds[0]
    feed.update(
        dataset_key="tw_equity_eod",
        source_timezone="Asia/Taipei",
        scheduled_time="18:00",
        target_date_lag_days=0,
        completion_window_seconds=14400,
    )
    runtime.installed_feeds = [feed]
    runtime._ensure_proof = lambda: None
    source.request = lambda *args, **kwargs: {
        "acquisition_allowed": False,
        "admitted_dataset_keys": [],
    }
    owner = SimpleNamespace(
        work={
            "kind": "daily",
            "dataset_key": feed["dataset_key"],
            "state": "open",
            "trade_date": str(date(2026, 10, 23)),
            "scope": {"feed": feed},
        }
    )

    @contextmanager
    def claimed(*args, **kwargs):
        yield owner

    monkeypatch.setattr(provider_work, "claimed_work", claimed)
    runtime._plan_for_date = lambda *args: (_ for _ in ()).throw(error)
    settlements = []
    runtime._settle_queued_round = lambda *args, **kwargs: settlements.append(kwargs)

    runtime._queued_cycle()

    assert settlements == [expected]
    runtime.close()
