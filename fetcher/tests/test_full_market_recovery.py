"""Frozen plan, independent catchup and persistent SDK recovery regressions."""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.full_market_universe import canonical_bytes
from findb_fetcher.providers.shioaji import ShioajiKbarsSnapshot, ShioajiSdkError
from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway
from test_full_market import _member, _runtime


def _frozen_plan(dataset: str, day: date, release: dict[str, Any]) -> dict[str, Any]:
    members = [row["member_key"] for row in release["members"]]
    identity = f"{dataset}:{day}:{release['release_id']}"
    return {
        "plan_id": identity,
        "provider": "twelve_data",
        "dataset_key": dataset,
        "trade_date": str(day),
        "release_id": release["release_id"],
        "deadline_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "status": "incomplete",
        "parts": [{"work_item_id": identity + ":part", "member_keys": members}],
        "summary": {
            "expected": len(members),
            "data": 0,
            "no_data": 0,
            "missing": len(members),
            "blocked": 0,
            "is_late": True,
        },
    }


def _release(identity: str, dataset: str, members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "release_id": identity,
        "provider": "twelve_data",
        "dataset_key": dataset,
        "status": "published",
        "members": members,
    }


class _Governance:
    def __init__(self, runtime: Any, releases: list[dict[str, Any]], activation: date) -> None:
        self.runtime = runtime
        self.releases = {release["release_id"]: release for release in releases}
        self.current = {release["dataset_key"]: release["release_id"] for release in releases}
        self.activation = activation
        self.plans: dict[tuple[str, str], dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.outcomes: list[dict[str, Any]] = []

    def universes(self, dataset: str, provider: str, as_of: str | None = None) -> dict[str, Any]:
        return {
            "activation": {"enabled": True, "activation_date": str(self.activation)},
            "published_release_id": self.current[dataset],
        }

    def release(self, identity: str) -> dict[str, Any]:
        return self.releases[identity]

    def plans_for_date(self, *, dataset: str, trade_date: str) -> dict[str, Any]:
        value = self.plans.get((dataset, trade_date))
        return {"data": [value] if value else []}

    def plan(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        plan = _frozen_plan(
            kwargs["dataset"],
            date.fromisoformat(kwargs["trade_date"]),
            self.release(kwargs["release_id"]),
        )
        self.plans[(kwargs["dataset"], kwargs["trade_date"])] = plan
        return plan

    def outcome(self, plan_id: str, **kwargs: Any) -> dict[str, Any]:
        self.outcomes.append({"plan_id": plan_id, **kwargs})
        return {}

    def evaluate(self, identity: str) -> dict[str, Any]:
        plan = next(plan for plan in self.plans.values() if plan["plan_id"] == identity)
        with self.runtime.state.connection() as db:
            rows = db.execute(
                "SELECT status FROM full_work WHERE plan_id=?", (identity,)
            ).fetchall()
        data = sum(row["status"] == "complete" for row in rows)
        blocked = sum(row["status"] == "manual" for row in rows)
        plan["summary"].update(data=data, blocked=blocked, missing=len(rows) - data - blocked)
        plan["status"] = "complete" if data == len(rows) else "incomplete"
        return plan


def _cycle_runtime(
    tmp_path: Path, releases: list[dict[str, Any]], days: list[date]
) -> tuple[Any, Any]:
    runtime, _, _ = _runtime(tmp_path)
    runtime.feeds[0].update(
        source_timezone="UTC",
        scheduled_time="00:00",
        target_date_lag_days=0,
        completion_window_seconds=86400,
    )
    source = _Governance(runtime, releases, min(days))
    runtime.source = source
    runtime._ensure_proof = lambda: None
    now = datetime.now(timezone.utc)
    runtime.state.refresh(
        "twelve_data", now=now.timestamp(), result=[], next_at=now.timestamp() + 86400
    )
    runtime.calendar = SimpleNamespace(
        get_year=lambda *args: SimpleNamespace(
            days=[SimpleNamespace(trade_date=day, is_open=True) for day in days]
        )
    )
    runtime.delivery = SimpleNamespace(
        prepare=lambda body: body,
        deliver=lambda body: SimpleNamespace(run_id=UUID(int=1)),
        get_run_status=lambda *args, **kwargs: SimpleNamespace(
            status="completed", failed_records=0
        ),
    )
    return runtime, source


@pytest.mark.parametrize("has_local_state", [False, True])
def test_frozen_hundred_member_plan_survives_same_date_publication_and_restart(
    tmp_path: Path, has_local_state: bool
) -> None:
    today = datetime.now(timezone.utc).date()
    old_day = today - timedelta(days=1)
    members = [
        {**_member(), "symbol": f"S{i}", "member_key": f"S{i}", "provider_symbol": f"OLD{i}"}
        for i in range(100)
    ]
    old = _release("original", "us_equity_eod", members)
    revised = _release(
        "new-mapping",
        "us_equity_eod",
        [{**member, "provider_symbol": f"NEW{i}"} for i, member in enumerate(members)],
    )
    runtime, source = _cycle_runtime(tmp_path, [old, revised], [old_day, today])
    frozen = _frozen_plan("us_equity_eod", old_day, old)
    source.plans[("us_equity_eod", str(old_day))] = copy.deepcopy(frozen)
    key = frozen["parts"][0]["work_item_id"] + ":S0"
    if has_local_state:
        runtime.state.record_plan(frozen, "twelve_data")
        runtime.state.prepare(key, canonical_bytes([{"provider_symbol": "OLD0"}]))
    runtime.state = FullMarketState(runtime.state.path)
    acquired: list[tuple[str, str]] = []
    runtime._acquire = lambda member, plan, *args: (
        acquired.append((plan["trade_date"], member["provider_symbol"]))
        or [{"symbol": member["symbol"]}]
    )
    runtime.cycle()
    assert source.plans[("us_equity_eod", str(old_day))]["parts"] == frozen["parts"]
    assert source.plans[("us_equity_eod", str(old_day))]["release_id"] == "original"
    assert [(day, symbol) for day, symbol in acquired if day == str(old_day)] == [
        (str(old_day), f"OLD{i}") for i in range(int(has_local_state), 100)
    ]
    assert [(day, symbol) for day, symbol in acquired if day == str(today)] == [
        (str(today), f"NEW{i}") for i in range(100)
    ]
    assert [call["release_id"] for call in source.created] == ["new-mapping"]
    if has_local_state:
        assert runtime.state.prepared(key) == canonical_bytes([{"provider_symbol": "OLD0"}])
    runtime.state = FullMarketState(runtime.state.path)
    runtime.cycle()
    assert len(acquired) == 200 - int(has_local_state)
    runtime.http.close()


def test_source_exact_date_lookup_recovers_plan_outside_latest_hundred() -> None:
    day = date(2026, 1, 1)
    plans = [
        {"trade_date": str(day + timedelta(days=i)), "release_id": f"r{i}"} for i in range(150)
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.params["dataset_key"] == "us_equity_eod"
        assert request.url.params["limit"] == "1"
        matching = [
            plan
            for plan in plans
            if request.url.params["start_date"]
            <= plan["trade_date"]
            <= request.url.params["end_date"]
        ]
        return httpx.Response(200, json={"data": matching[:1]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        source = FullMarketSourceClient(
            FetcherConfig("https://source.example", "secret"), client=client
        )
        assert source.plans_for_date(dataset="us_equity_eod", trade_date=str(day))["data"] == [
            plans[0]
        ]


def test_concurrent_plan_freeze_recovers_original_release_on_conflict(tmp_path: Path) -> None:
    day = datetime.now(timezone.utc).date()
    old, latest = [_release(identity, "us_equity_eod", [_member()]) for identity in ("old", "new")]
    runtime, source = _cycle_runtime(tmp_path, [old, latest], [day])
    frozen = _frozen_plan("us_equity_eod", day, old)

    def racing_plan(**kwargs: Any) -> Any:
        source.plans[("us_equity_eod", str(day))] = frozen
        raise FullMarketProtocolError("HTTP409", status_code=409)

    source.plan = racing_plan
    assert runtime._plan_for_date(runtime.feeds[0], day) == frozen
    runtime.http.close()


@pytest.mark.parametrize("quota_exhausted", [False, True])
def test_latest_manual_gap_does_not_starve_older_shared_us_hk_work(
    tmp_path: Path, quota_exhausted: bool
) -> None:
    today = datetime.now(timezone.utc).date()
    older = today - timedelta(days=1)
    us = _release("us", "us_equity_eod", [_member()])
    hk = _release("hk", "hk_equity_eod", [{**_member(), "symbol": "00700", "member_key": "00700"}])
    runtime, source = _cycle_runtime(tmp_path, [us, hk], [older, today])
    runtime.feeds.append({**runtime.feeds[0], "dataset_key": "hk_equity_eod", "market": "HK"})
    latest = _frozen_plan("us_equity_eod", today, us)
    # A manual-only latest plan near its deadline needs zero new provider calls.
    latest["deadline_at"] = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    source.plans[("us_equity_eod", str(today))] = latest
    runtime.state.record_plan(latest, "twelve_data")
    runtime.state.finish(
        latest["parts"][0]["work_item_id"] + ":AAPL", status="manual", reason="mapping_gap"
    )
    acquired: list[tuple[str, str]] = []
    attempts: list[tuple[str, str]] = []
    runtime.proof["requests_per_second"] = 100000

    def acquire(member: Any, plan: Any, *args: Any) -> list[dict[str, Any]]:
        attempts.append((plan["dataset_key"], plan["trade_date"]))
        runtime._reserve()
        acquired.append(attempts[-1])
        return [{"symbol": member["symbol"]}]

    runtime._acquire = acquire
    if quota_exhausted:
        runtime.state.reserve(
            account="twelve_data",
            window=str(today),
            requests=runtime.proof["requests_per_day"],
            byte_count=0,
            request_limit=runtime.proof["requests_per_day"],
            byte_limit=1,
            now=datetime.now(timezone.utc).timestamp(),
        )
    health = runtime.cycle()
    if quota_exhausted:
        assert acquired == []
        assert attempts == [("hk_equity_eod", str(today))]
        assert source.outcomes[-1]["reason"] == "rate_limited"
    else:
        assert acquired == [
            ("hk_equity_eod", str(today)),
            ("us_equity_eod", str(older)),
            ("hk_equity_eod", str(older)),
        ]
    assert health["manual_required"] == 1
    assert all(trade_date >= str(older) for _, trade_date in source.plans)
    assert latest["plan_id"] in [plan["plan_id"] for plan in health["unresolved"]]
    runtime.http.close()


@pytest.mark.parametrize("failure", ["timeout", "dead-child"])
def test_shioaji_failed_child_recreated_shared_feeds_and_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    runtime, _, _ = _runtime(tmp_path, provider="shioaji")
    day = datetime.now(timezone.utc).date()
    plan = _frozen_plan("tw_equity_minute", day, _release("tw", "tw_equity_minute", [_member()]))
    nanos = (
        int(
            (
                datetime.combine(day, datetime.min.time())
                + timedelta(hours=9, minutes=1)
                - datetime(1970, 1, 1)
            ).total_seconds()
        )
        * 1000000000
    )
    snapshot = ShioajiKbarsSnapshot(
        {
            "ts": (nanos,),
            "Open": (100,),
            "High": (101,),
            "Low": (99,),
            "Close": (100,),
            "Volume": (1,),
            "Amount": (100000,),
        },
        100,
        200,
    )
    gateways: list[Any] = []

    class Gateway:
        def __init__(self, *args: Any) -> None:
            self.closed = False
            self.calls: list[str] = []
            gateways.append(self)

        def is_alive(self) -> bool:
            return not self.closed

        def fetch_kbars(self, symbol: str, target: date) -> Any:
            self.calls.append(symbol)
            if len(gateways) == 1 and failure == "timeout":
                self.closed = True
                raise ShioajiSdkError("isolated failure", code="ACQUISITION")
            return snapshot

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "findb_fetcher.full_market_runtime.PersistentIsolatedShioajiGateway", Gateway
    )
    runtime.proof["requests_per_second"] = 100000
    member = {**_member(), "symbol": "2330", "provider_symbol": "2330", "member_key": "2330"}
    if failure == "dead-child":
        runtime.sdk = Gateway()
        runtime.sdk.closed = True
    else:
        with pytest.raises(ShioajiSdkError):
            runtime._acquire(member, plan, plan["parts"][0], {"release_id": "tw"}, runtime.feeds[0])
        assert runtime.sdk is None
    first = runtime._acquire(member, plan, plan["parts"][0], {"release_id": "tw"}, runtime.feeds[0])
    etf = {**member, "symbol": "00631L", "provider_symbol": "00631L", "member_key": "00631L"}
    plan["dataset_key"] = "tw_etf_minute"
    second = runtime._acquire(etf, plan, plan["parts"][0], {"release_id": "etf"}, runtime.feeds[0])
    assert len(gateways) == 2 and gateways[0].closed
    assert gateways[1].calls == ["2330", "00631L"]
    assert first[0]["dataset_key"] == "tw_equity_minute"
    assert second[0]["dataset_key"] == "tw_etf_minute"
    runtime.can_acquire = lambda: False
    runtime.pause()
    assert runtime.sdk is None and gateways[1].closed
    with pytest.raises(Exception, match="stopped"):
        runtime._acquire(etf, plan, plan["parts"][0], {"release_id": "etf"}, runtime.feeds[0])
    assert len(gateways) == 2
    runtime.http.close()


@pytest.mark.parametrize("failure", ["timeout", "dead-child"])
def test_persistent_gateway_transport_failure_closes_boundary(failure: str) -> None:
    gateway = PersistentIsolatedShioajiGateway.__new__(PersistentIsolatedShioajiGateway)
    gateway.timeout_seconds = 0

    class Connection:
        closed = False

        def send_bytes(self, *args: Any) -> None:
            pass

        def poll(self, *args: Any) -> bool:
            return False

        def close(self) -> None:
            self.closed = True

    class Process:
        running = failure != "dead-child"

        def is_alive(self) -> bool:
            return self.running

        def join(self, *args: Any) -> None:
            pass

        def terminate(self) -> None:
            self.running = False

    gateway.parent = Connection()
    gateway.process = Process()
    with pytest.raises(ShioajiSdkError):
        gateway.fetch_kbars("2330", date(2026, 10, 1))
    assert gateway.parent.closed
    assert not gateway.process.is_alive()
    assert not gateway.is_alive()
