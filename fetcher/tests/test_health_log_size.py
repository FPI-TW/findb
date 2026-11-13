"""The per-cycle health line stays small however many members a plan has failed."""

import json
import threading
from types import SimpleNamespace

from findb_fetcher import full_market_cli
from findb_fetcher.full_market_cli import HEALTH_LINE_LIMIT_BYTES, health_line
from findb_fetcher.full_market_state import HEALTH_FAILURE_SAMPLES, FullMarketState


def _state(tmp_path, *, plans: int, gaps: int) -> FullMarketState:
    """Unfinished plans whose first ``gaps`` members are permanent mapping gaps."""
    state = FullMarketState(tmp_path / "state.sqlite3")
    for index in range(plans):
        plan_id = f"00000000-0000-7000-8000-{index:012d}"
        members = [f"M{member:05d}" for member in range(gaps + 5)]
        state.record_plan(
            {
                "plan_id": plan_id,
                "dataset_key": "us_equity_eod",
                "trade_date": f"2026-11-{index + 1:02d}",
                "release_id": "release",
                "status": "incomplete",
                "parts": [{"work_item_id": f"part-{index}", "member_keys": members}],
            },
            "twelve_data",
        )
        with state.connection() as db:
            db.execute(
                "UPDATE full_work SET status='manual',reason='mapping_gap' "
                "WHERE plan_id=? AND member_key<?",
                (plan_id, members[gaps]),
            )
    return state


def test_health_reports_failure_counts_and_a_bounded_sample(tmp_path):
    state = _state(tmp_path, plans=12, gaps=1000)
    health = state.health("twelve_data")

    assert len(health["plans"]) == 12
    assert all(plan["failure_counts"] == {"mapping_gap": 1000} for plan in health["plans"])
    assert all(plan["counts"] == {"manual": 1000, "pending": 5} for plan in health["plans"])
    # The examples are shared by the whole report, earliest plan first.
    assert [len(plan["failures"]) for plan in health["plans"]] == [HEALTH_FAILURE_SAMPLES] + [
        0
    ] * 11
    assert health["plans"][0]["failures"][0] == {
        "member_key": "M00000",
        "reason": "mapping_gap",
        "attempts": 0,
    }
    line = health_line(health)
    assert len(line.encode()) < HEALTH_LINE_LIMIT_BYTES and "truncated" not in json.loads(line)
    # Settlement proof still names every failed member of its own plan.
    assert len(state.plan_progress(health["plans"][3]["plan_id"])["failures"]) == 1000


def test_health_line_size_does_not_follow_the_number_of_failed_members(tmp_path):
    few = health_line(_state(tmp_path / "few", plans=12, gaps=20).health("twelve_data"))
    many = health_line(_state(tmp_path / "many", plans=12, gaps=1000).health("twelve_data"))
    assert abs(len(many) - len(few)) < 200


def test_health_line_drops_detail_before_exceeding_its_limit(tmp_path):
    health = _state(tmp_path, plans=12, gaps=30).health("twelve_data")
    full = len(health_line(health).encode())

    without_samples = json.loads(health_line(health, limit=full - 1))
    assert without_samples["truncated"] is True
    assert all("failures" not in plan for plan in without_samples["plans"])
    assert without_samples["plans"][0]["failure_counts"] == {"mapping_gap": 30}

    counts_only = health_line(health, limit=600)
    assert len(counts_only.encode()) <= 600
    reduced = json.loads(counts_only)
    assert reduced["truncated"] is True and reduced["plans_count"] == 12
    assert reduced["unresolved_count"] == 12 and "plans" not in reduced
    assert reduced["provider"] == "twelve_data" and reduced["manual_required"] == 360


def test_worker_repeats_an_unchanged_health_report_only_at_the_interval(monkeypatch, capsys):
    monkeypatch.setattr(full_market_cli, "prune_ledger", lambda: None)
    clock = [1000.0]
    monkeypatch.setattr(full_market_cli.time, "monotonic", lambda: clock[0])
    stopped = threading.Event()
    # Each cycle: (seconds since the previous one, unresolved plans it reports).
    cycles = iter([(0, 0), (30, 0), (30, 0), (30, 1), (30, 1), (600, 1), (30, 1)])

    def cycle() -> dict:
        elapsed, unresolved = next(cycles)
        clock[0] += elapsed
        return {
            "provider": "twelve_data",
            "unresolved": [{"plan_id": "p"}] * unresolved,
            # The clock of the report itself never makes it a new one.
            "observed_at": f"2026-11-13T00:00:{clock[0] % 60:02.0f}+00:00",
        }

    class Stopped:
        def wait(self, seconds: float) -> None:
            if not next(remaining, 0):
                stopped.set()

        def is_set(self) -> bool:
            return stopped.is_set()

    remaining = iter([1] * 6)
    runtime = SimpleNamespace(
        heartbeat=None,
        prune_closed_state=lambda: None,
        drain=lambda: None,
        pause=lambda: None,
        cycle=cycle,
    )
    control = SimpleNamespace(poll=lambda **_: SimpleNamespace(desired_state="running"))

    full_market_cli.run_worker(
        runtime, control, lambda: True, stopped=Stopped(), run_forever=True, poll_seconds=1
    )

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    # First report, the change to one unresolved plan, and the repeat after the interval.
    assert [len(line["unresolved"]) for line in lines] == [0, 1, 1]
