"""Universe refreshes follow the daily schedule, not their own completion time."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from findb_fetcher import full_market_runtime as module
from findb_fetcher import provider_work
from findb_fetcher.execution_config import ExecutionSnapshot
from test_full_market import _runtime

TAIPEI = timezone(timedelta(hours=8))
FEEDS = tuple(
    {
        "provider": "finlab",
        "dataset_key": key,
        "source_timezone": "Asia/Taipei",
        "scheduled_time": "18:00",
        "target_date_lag_days": 0,
    }
    for key in ("tw_equity_eod", "tw_etf_eod")
)


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TAIPEI).astimezone(timezone.utc)


@pytest.mark.parametrize(
    ("clock", "latest", "upcoming"),
    [
        ("2026-11-13T17:59:59", "2026-11-12T18:00:00", "2026-11-13T18:00:00"),
        ("2026-11-13T18:00:00", "2026-11-13T18:00:00", "2026-11-14T18:00:00"),
        ("2026-11-14T09:11:00", "2026-11-13T18:00:00", "2026-11-14T18:00:00"),
    ],
)
def test_refresh_slots_are_the_scheduled_times_around_the_clock(clock, latest, upcoming):
    assert module.refresh_slots(FEEDS, _at(clock)) == (_at(latest), _at(upcoming))


def test_refresh_slots_keep_the_wall_clock_across_a_time_change_and_need_a_schedule():
    feed = {"source_timezone": "America/New_York", "scheduled_time": "17:00"}
    # 2026-11-01 ends US daylight time: the next 17:00 is 25 hours later.
    latest, upcoming = module.refresh_slots(
        [feed], datetime(2026, 10, 31, 21, 30, tzinfo=timezone.utc)
    )
    assert latest == datetime(2026, 10, 31, 21, tzinfo=timezone.utc)
    assert upcoming == datetime(2026, 11, 1, 22, tzinfo=timezone.utc)
    assert module.refresh_slots([], datetime(2026, 11, 1, tzinfo=timezone.utc)) is None


def _cycle_runtime(tmp_path, monkeypatch, clock):
    runtime, source, _ = _runtime(tmp_path, provider="finlab")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz)

    monkeypatch.setattr(module, "datetime", Clock)
    runtime.installed_feeds = list(FEEDS)
    runtime._ensure_proof = lambda: None
    source.request = lambda method, path, body=None: {
        "acquisition_allowed": True,
        "admitted_dataset_keys": [feed["dataset_key"] for feed in FEEDS],
    }
    source.universes = lambda *args, **kwargs: {
        "activation": {"enabled": True, "activation_date": "2026-11-12"},
        "published_release_id": "frozen",
    }
    enqueued: list[tuple[str, str]] = []
    runtime._enqueue = lambda body: enqueued.append((body["kind"], body["stable_key"]))

    @contextmanager
    def idle(*args, **kwargs):
        yield None

    monkeypatch.setattr(provider_work, "claimed_work", idle)
    return runtime, enqueued


def test_scheduled_round_enqueues_the_refresh_ahead_of_that_days_plans(tmp_path, monkeypatch):
    clock = [_at("2026-11-13T17:59:00")]
    runtime, enqueued = _cycle_runtime(tmp_path, monkeypatch, clock)
    runtime.state.refresh(
        "finlab", now=0, result=[], next_at=_at("2026-11-13T18:00:00").timestamp()
    )
    try:
        snapshot = ExecutionSnapshot("fixture", FEEDS)
        runtime._queued_cycle(snapshot=snapshot)
        # Before the scheduled time only the previous date is due and no refresh is.
        assert [kind for kind, _ in enqueued] == ["daily", "daily"]
        enqueued.clear()
        clock[0] = _at("2026-11-13T18:00:00")
        runtime._queued_cycle(snapshot=snapshot)
        assert enqueued[:2] == [
            ("maintenance", "maintenance:tw_equity_eod:2026-11-13T10:00Z"),
            ("maintenance", "maintenance:tw_etf_eod:2026-11-13T10:00Z"),
        ]
        assert [key for kind, key in enqueued[2:] if key.endswith(":2026-11-13")] == [
            "daily:full_market:tw_equity_eod:2026-11-13",
            "daily:full_market:tw_etf_eod:2026-11-13",
        ]
        # A refresh still pending or failed keeps its identity until the next scheduled time.
        enqueued.clear()
        clock[0] = _at("2026-11-14T09:00:00")
        runtime._queued_cycle(snapshot=snapshot)
        assert enqueued[0] == ("maintenance", "maintenance:tw_equity_eod:2026-11-13T10:00Z")
        enqueued.clear()
        clock[0] = _at("2026-11-14T18:00:30")
        runtime._queued_cycle(snapshot=snapshot)
        assert enqueued[0] == ("maintenance", "maintenance:tw_equity_eod:2026-11-14T10:00Z")
    finally:
        runtime.http.close()


@pytest.mark.parametrize(
    ("completed", "due"),
    [
        # Refreshes during the day, after a late start or by hand, return to the schedule.
        ("2026-11-13T09:13:00", "2026-11-13T18:00:00"),
        ("2026-11-13T18:01:30", "2026-11-14T18:00:00"),
        ("2026-11-13T23:40:00", "2026-11-14T18:00:00"),
    ],
)
def test_completed_refresh_is_next_due_at_the_scheduled_time(tmp_path, monkeypatch, completed, due):
    clock = [_at(completed)]
    runtime, _ = _cycle_runtime(tmp_path, monkeypatch, clock)
    runtime.schedule_snapshot = ExecutionSnapshot("fixture", FEEDS)
    results = [{"dataset_key": "tw_equity_eod", "status": "published"}]
    runtime.sync_universes = lambda **kwargs: results
    runtime.pause = lambda: None
    settled = []
    owner = SimpleNamespace(
        work={
            "work_id": "refresh",
            "kind": "maintenance",
            "dataset_key": "tw_equity_eod",
            "state": "open",
        },
        journal=SimpleNamespace(reserve_attempt=lambda *args: None),
        dispatch_observations=0,
        freeze=lambda manifest: None,
        settle=lambda outcome, **kwargs: settled.append(outcome),
    )
    try:
        assert runtime._run_queued_maintenance(owner) == results
        assert settled == ["completed"]
        assert not runtime.state.refresh_due("finlab", _at(due).timestamp() - 1)
        assert runtime.state.refresh_due("finlab", _at(due).timestamp())
    finally:
        runtime.http.close()


def test_refresh_without_a_published_schedule_keeps_the_daily_interval(tmp_path, monkeypatch):
    clock = [_at("2026-11-13T09:13:00")]
    runtime, _ = _cycle_runtime(tmp_path, monkeypatch, clock)
    try:
        assert runtime._next_refresh_at(clock[0]) == clock[0].timestamp() + 86400
        # A refresh run while draining still knows the schedule it resolved its dates from.
        runtime.refresh_snapshot = ExecutionSnapshot("fixture", FEEDS)
        assert runtime._next_refresh_at(clock[0]) == _at("2026-11-13T18:00:00").timestamp()
    finally:
        runtime.http.close()
