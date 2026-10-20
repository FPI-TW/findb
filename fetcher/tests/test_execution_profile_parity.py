"""The environment and execution profile must not alter scheduler date decisions."""

import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from findb_fetcher.full_market_runtime import ReadinessBlockedError
from test_full_market import _member, _runtime
from test_full_market_recovery import _frozen_plan, _release

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
FEEDS = json.loads((CONFIGS / "full_market.staging.v1.json").read_text())["feeds"]


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("feed", FEEDS, ids=lambda feed: feed["dataset_key"])
def test_same_calendar_date_retry_and_frozen_membership(
    tmp_path, monkeypatch, environment, profile, feed
):
    import findb_fetcher.full_market_runtime as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 22, 22, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(module, "datetime", Clock)
    manifest = json.loads((CONFIGS / f"full_market.{environment}.v1.json").read_text())
    selected = next(row for row in manifest["feeds"] if row["dataset_key"] == feed["dataset_key"])
    assert selected == feed
    runtime, source, _ = _runtime(tmp_path, provider=feed["provider"])
    runtime.execution_profile = profile
    runtime.installed_feeds = runtime.feeds = [selected]
    runtime._ensure_proof = lambda: None
    runtime.state.bind_profile(profile)
    now = Clock.now(timezone.utc).timestamp()
    runtime.state.refresh(feed["provider"], now=now, result=[], next_at=now + 86400)
    members = [
        {**_member(), "member_key": symbol, "symbol": symbol, "provider_symbol": symbol}
        for symbol in ("A", "B", "C")
    ]
    release = _release("published-original", feed["dataset_key"], members)
    release["provider"] = feed["provider"]
    frozen = {}
    for day in (date(2026, 7, 21), date(2026, 7, 22)):
        plan = _frozen_plan(feed["dataset_key"], day, release)
        plan.update(provider=feed["provider"], execution_profile=profile)
        plan["parts"][0]["member_keys"] = [
            row["member_key"] for row in members[: 2 if profile == "pilot" else 3]
        ]
        frozen[str(day)] = plan
    source.request = lambda method, path: {
        "acquisition_allowed": True,
        "admitted_dataset_keys": [feed["dataset_key"]],
    }
    source.universes = lambda *args, **kwargs: {
        "activation": {"enabled": True, "activation_date": "2026-07-21"},
        "published_release_id": release["release_id"],
    }
    source.release = lambda identity: release
    source.plans_for_date = lambda **kwargs: {"data": [copy.deepcopy(frozen[kwargs["trade_date"]])]}
    source.evaluate = lambda identity: copy.deepcopy(
        next(plan for plan in frozen.values() if plan["plan_id"] == identity)
    )
    runtime.calendar = SimpleNamespace(
        get_year=lambda *args: SimpleNamespace(
            days=[
                SimpleNamespace(trade_date=date(2026, 7, day), is_open=day != 20)
                for day in (20, 21, 22, 23)
            ]
        )
    )
    seen = []

    def deliver(plan, pinned, current_feed, **kwargs):
        if not kwargs.get("drain_only"):
            seen.append((plan["trade_date"], plan["parts"][0]["member_keys"], pinned["release_id"]))
        return True

    runtime._deliver_plan = deliver
    if feed["provider"] == "factset":
        # FactSet has no legacy, unfenced acquisition path in either profile.
        runtime.cycle()
        runtime.drain()
        assert seen == []
        return
    for _ in range(2):
        runtime.cycle()
        # New universe members cannot change either profile's existing plans.
        release["members"].append({**_member(), "member_key": "D", "symbol": "D"})
    expected_members = ["A", "B"] if profile == "pilot" else ["A", "B", "C"]
    assert seen == [
        (day, expected_members, "published-original")
        for day in ("2026-07-22", "2026-07-21", "2026-07-22", "2026-07-21")
    ]
    frozen["2026-07-22"]["execution_profile"] = "full_market" if profile == "pilot" else "pilot"
    with pytest.raises(ReadinessBlockedError, match="identity invalid"):
        runtime._existing_plan(selected, date(2026, 7, 22))
