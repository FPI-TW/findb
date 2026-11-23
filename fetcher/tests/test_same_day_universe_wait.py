"""Today's plan waits, within a bound, for today's universe refresh (#598)."""

import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import findb_fetcher.full_market_runtime as runtime_module
from findb_fetcher.full_market_errors import UniverseRefreshPendingError
from findb_fetcher.full_market_runtime import (
    SAME_DAY_UNIVERSE_RECHECK_SECONDS,
    SAME_DAY_UNIVERSE_WAIT_SECONDS,
)
from test_full_market import _runtime

DAY = date(2026, 11, 17)
# 17:00 New York on DAY.
SCHEDULED = datetime(2026, 11, 17, 22, 0, tzinfo=timezone.utc)


def _freeze_clock(monkeypatch, at: datetime) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz) if tz else at.replace(tzinfo=None)

    monkeypatch.setattr(runtime_module, "datetime", Clock)


def _setup(tmp_path, monkeypatch, *, at: datetime, effective: date, provider="twelve_data"):
    _freeze_clock(monkeypatch, at)
    runtime, source, _ = _runtime(tmp_path, provider=provider)
    feed = {
        **runtime.feeds[0],
        "source_timezone": "America/New_York",
        "scheduled_time": "17:00",
        "target_date_lag_days": 0,
    }
    source.plans_for_date = lambda **kw: {"data": []}
    source.universes = lambda *a, **kw: {"published_release_id": "latest"}
    source.release = lambda identity: {
        "release_id": identity,
        "provider": provider,
        "dataset_key": feed["dataset_key"],
        "effective_date": effective.isoformat(),
        "status": "published",
    }
    created = []

    def plan(**kw):
        created.append(kw)
        return {
            "plan_id": "p",
            "provider": provider,
            "execution_profile": runtime.execution_profile,
            "dataset_key": kw["dataset"],
            "trade_date": kw["trade_date"],
            "release_id": kw["release_id"],
            "status": "incomplete",
        }

    source.plan = plan
    return runtime, feed, created


@pytest.mark.parametrize(
    "elapsed,expected_wait",
    [
        (timedelta(minutes=1), SAME_DAY_UNIVERSE_RECHECK_SECONDS),
        (timedelta(minutes=28), 120),
        # Never sooner than a minute, even right at the bound.
        (timedelta(seconds=SAME_DAY_UNIVERSE_WAIT_SECONDS - 10), 60),
    ],
)
def test_today_waits_for_todays_release_without_freezing_a_plan(
    tmp_path, monkeypatch, elapsed, expected_wait
):
    runtime, feed, created = _setup(
        tmp_path, monkeypatch, at=SCHEDULED + elapsed, effective=DAY - timedelta(days=1)
    )
    try:
        with pytest.raises(UniverseRefreshPendingError) as raised:
            runtime._plan_for_date(feed, DAY)
        assert raised.value.retry_after_seconds == expected_wait
        assert created == []
    finally:
        runtime.close()


def test_todays_release_is_frozen_at_once(tmp_path, monkeypatch):
    runtime, feed, created = _setup(
        tmp_path, monkeypatch, at=SCHEDULED + timedelta(minutes=1), effective=DAY
    )
    try:
        assert runtime._plan_for_date(feed, DAY)["release_id"] == "latest"
        assert [row["release_id"] for row in created] == ["latest"]
    finally:
        runtime.close()


def test_past_the_bound_the_latest_release_is_frozen_and_logged(tmp_path, monkeypatch, capsys):
    runtime, feed, created = _setup(
        tmp_path,
        monkeypatch,
        at=SCHEDULED + timedelta(seconds=SAME_DAY_UNIVERSE_WAIT_SECONDS),
        effective=DAY - timedelta(days=1),
    )
    try:
        assert runtime._plan_for_date(feed, DAY)["release_id"] == "latest"
        logged = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if "universe_refresh_stale" in line
        ]
        assert logged == [
            {
                "universe_refresh_stale": {
                    "dataset": feed["dataset_key"],
                    "trade_date": DAY.isoformat(),
                    "release_effective_date": (DAY - timedelta(days=1)).isoformat(),
                }
            }
        ]
    finally:
        runtime.close()


def test_a_refresh_completed_since_the_scheduled_time_ends_the_wait(tmp_path, monkeypatch):
    """A weekend or holiday list keeps the last trading day's date: waiting cannot help.

    Production 2026-11-22 (Sunday): the 17:00 refresh completed with the Nasdaq file
    of Friday, and the daily waited the whole bound before settling the closed date.
    """
    runtime, feed, created = _setup(
        tmp_path,
        monkeypatch,
        at=SCHEDULED + timedelta(minutes=1),
        effective=DAY - timedelta(days=2),
    )
    runtime.state.record_universe_refreshed(
        runtime.provider, feed["dataset_key"], now=(SCHEDULED + timedelta(seconds=11)).timestamp()
    )
    try:
        assert runtime._plan_for_date(feed, DAY)["release_id"] == "latest"
        assert len(created) == 1
    finally:
        runtime.close()


def test_only_this_feeds_refresh_since_the_scheduled_time_ends_the_wait(tmp_path, monkeypatch):
    runtime, feed, created = _setup(
        tmp_path,
        monkeypatch,
        at=SCHEDULED + timedelta(minutes=1),
        effective=DAY - timedelta(days=1),
    )
    # Yesterday's refresh of this feed, and today's of another, leave today's pending.
    runtime.state.record_universe_refreshed(
        runtime.provider, feed["dataset_key"], now=(SCHEDULED - timedelta(days=1)).timestamp()
    )
    runtime.state.record_universe_refreshed(
        runtime.provider, "other_dataset", now=SCHEDULED.timestamp()
    )
    try:
        with pytest.raises(UniverseRefreshPendingError):
            runtime._plan_for_date(feed, DAY)
        assert created == []
    finally:
        runtime.close()


def test_a_refresh_time_never_moves_back(tmp_path, monkeypatch):
    runtime, feed, _ = _setup(tmp_path, monkeypatch, at=SCHEDULED, effective=DAY)
    try:
        for at in (SCHEDULED, SCHEDULED - timedelta(hours=1)):
            runtime.state.record_universe_refreshed(
                runtime.provider, feed["dataset_key"], now=at.timestamp()
            )
        assert runtime.state.universe_refreshed_at(runtime.provider, feed["dataset_key"]) == (
            SCHEDULED.timestamp()
        )
    finally:
        runtime.close()


def test_a_failed_refresh_settles_with_its_diagnostic_in_the_proof(tmp_path, monkeypatch):
    runtime, feed, _ = _setup(tmp_path, monkeypatch, at=SCHEDULED, effective=DAY)
    settled = []
    owner = SimpleNamespace(
        work={"kind": "maintenance", "dataset_key": feed["dataset_key"], "state": "open"},
        request=lambda *args, **kwargs: None,
        journal=SimpleNamespace(manifest=lambda: None),
        settle=lambda outcome, **kwargs: settled.append((outcome, kwargs)),
    )
    runtime.pause = lambda: None
    transport = {"transport": {"origin": "source", "url": "/api/v1/source/universes"}}
    try:
        runtime._settle_queued_round(
            owner,
            outcome="retry",
            failure_code="MAINTENANCE_SOURCE_TRANSPORT",
            diagnostic=transport,
        )
        assert settled == [
            (
                "retry",
                {
                    "delay": 60,
                    "failure_code": "MAINTENANCE_SOURCE_TRANSPORT",
                    "proof": transport,
                },
            )
        ]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "at",
    [
        # A catch-up date: its own scheduled refresh is a day old.
        SCHEDULED + timedelta(days=1, minutes=1),
        # Before the scheduled time no refresh is due yet.
        SCHEDULED - timedelta(minutes=1),
    ],
)
def test_other_dates_never_wait(tmp_path, monkeypatch, at):
    runtime, feed, created = _setup(tmp_path, monkeypatch, at=at, effective=DAY - timedelta(days=1))
    try:
        runtime._plan_for_date(feed, DAY)
        assert len(created) == 1
    finally:
        runtime.close()


def test_providers_without_a_same_day_release_never_wait(tmp_path, monkeypatch):
    runtime, feed, created = _setup(
        tmp_path,
        monkeypatch,
        at=SCHEDULED + timedelta(minutes=1),
        effective=DAY - timedelta(days=1),
    )
    monkeypatch.setattr(runtime.adapter, "daily_waits_for_same_day_universe", False)
    try:
        runtime._plan_for_date(feed, DAY)
        assert len(created) == 1
    finally:
        runtime.close()


def test_waiting_providers_are_the_ones_with_same_day_releases():
    from findb_fetcher.provider_adapters import ADAPTERS

    # FactSet's HKEX list carries its own date and TAIFEX plans from a dated universe.
    assert {
        provider
        for provider, adapter in ADAPTERS.items()
        if adapter.daily_waits_for_same_day_universe
    } == {"finlab", "twelve_data"}


def test_a_waiting_daily_settles_as_a_retry_after_the_recheck(tmp_path, monkeypatch):
    from findb_fetcher import provider_work

    runtime, feed, _ = _setup(
        tmp_path, monkeypatch, at=SCHEDULED + timedelta(minutes=1), effective=DAY
    )
    owner = SimpleNamespace(
        work={
            "kind": "daily",
            "dataset_key": feed["dataset_key"],
            "state": "open",
            "trade_date": DAY.isoformat(),
            "scope": {"feed": feed},
        }
    )

    @contextmanager
    def claimed(*args, **kwargs):
        yield owner

    monkeypatch.setattr(provider_work, "claimed_work", claimed)
    runtime._ensure_proof = lambda: None
    runtime.source.request = lambda *a, **kw: {
        "acquisition_allowed": True,
        "admitted_dataset_keys": [feed["dataset_key"]],
    }
    runtime._plan_for_date = lambda *args: (_ for _ in ()).throw(
        UniverseRefreshPendingError(retry_after_seconds=SAME_DAY_UNIVERSE_RECHECK_SECONDS)
    )
    settlements = []
    runtime._settle_queued_round = lambda *args, **kwargs: settlements.append(kwargs)
    try:
        runtime._queued_cycle(plan_new_dates=False)
        assert settlements == [
            {
                "outcome": "retry",
                "failure_code": "DAILY_UNIVERSE_REFRESH_PENDING",
                "retry_delay_seconds": SAME_DAY_UNIVERSE_RECHECK_SECONDS,
            }
        ]
    finally:
        runtime.close()
