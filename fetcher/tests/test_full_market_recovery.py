"""Frozen plan, independent catchup and persistent SDK recovery regressions."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx

from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from test_full_market import _member, _runtime


def _frozen_plan(dataset: str, day: date, release: dict[str, Any]) -> dict[str, Any]:
    members = [row["member_key"] for row in release["members"]]
    identity = f"{dataset}:{day}:{release['release_id']}"
    return {
        "plan_id": identity,
        "provider": "twelve_data",
        "execution_profile": "full_market",
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

    def request(self, method: str, path: str) -> dict[str, Any]:
        assert method == "GET" and path == "full-market/authorization?consumer=full_market"
        return {"acquisition_allowed": True, "admitted_dataset_keys": list(self.current)}

    def authorization(self, *args: Any, **kwargs: Any):
        import time

        from findb_fetcher.acquisition import Authorization

        return Authorization(time.monotonic(), time.time(), time.time())

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
            status="completed", failed_records=0, total_records=1, success_records=1
        ),
    )
    return runtime, source


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
