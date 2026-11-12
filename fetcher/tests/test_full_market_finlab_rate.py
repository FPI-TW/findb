"""Actual FinLab acquisition pacing, durable quotas and prepared-body recovery."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import findb_fetcher.full_market_runtime as runtime_module
from findb_fetcher.client import SourceAPIClient, delivered_snapshot
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_runtime import load_readiness
from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.full_market_universe import canonical_bytes, checksum
from findb_fetcher.provider_wait import (
    CalendarDecisionRepairRequiredError,
    ProviderOutagePendingError,
    ProviderPublicationPendingError,
    ProviderWorkRejectedError,
)
from findb_fetcher.provider_work import WorkJournal
from findb_fetcher.providers.finlab import (
    FinLabDatasetTable,
    FinLabOperationalError,
    FinLabTargetNotReadyError,
)
from test_full_market import _checkpoint_reserve, _proof, _runtime


class _Clock:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.elapsed = 100.0
        self.origin = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        self.waits: list[float] = []
        clock = self

        class ClockDatetime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return (clock.origin + timedelta(seconds=clock.elapsed)).astimezone(tz)

        monkeypatch.setattr(runtime_module, "datetime", ClockDatetime)
        monkeypatch.setattr(runtime_module.time, "monotonic", lambda: self.elapsed)
        monkeypatch.setattr(runtime_module.time, "time", lambda: self.now().timestamp())
        monkeypatch.setattr(runtime_module.time, "sleep", self.sleep)

    def now(self) -> datetime:
        return self.origin + timedelta(seconds=self.elapsed)

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.elapsed += seconds


def _finlab_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dataset: str
) -> tuple[Any, Any, Any, _Clock, Any]:
    runtime, source, raw = _runtime(tmp_path, provider="finlab")
    clock = _Clock(monkeypatch)
    feed = runtime.feeds[0]
    feed.update(
        dataset_key=dataset,
        market="TW",
        asset_class="etf" if dataset == "tw_etf_eod" else "equity",
        slot_id="taiwan_market_window",
        completion_window_seconds=14400,
    )
    proof = {
        **_proof(clock.now()),
        "provider": "finlab",
        "datasets": [dataset],
        "requests_per_second": 1,
        "requests_per_minute": 60,
    }
    path = tmp_path / "readiness.json"
    path.write_bytes(canonical_bytes(proof))
    runtime.proof = load_readiness(path, "finlab", [dataset], now=clock.now())
    symbols = ("0050", "006208") if dataset == "tw_etf_eod" else ("2330", "2317")
    release = {
        "release_id": "release:" + dataset,
        "members": [
            {
                "symbol": symbol,
                "provider_symbol": symbol,
                "member_key": symbol,
                "mapping_status": "mapped",
            }
            for symbol in symbols
        ],
    }
    plan = {
        "plan_id": "plan:" + dataset,
        "dataset_key": dataset,
        "trade_date": "2026-10-01",
        "parts": [{"work_item_id": dataset, "member_keys": list(symbols)}],
    }
    runtime.state.record_plan(plan, "finlab")
    gateway = SimpleNamespace(calls=[], fail_at=None, failure="network")
    from findb_fetcher import acquisition

    platform_limits = acquisition.limits
    monkeypatch.setattr(
        acquisition,
        "limits",
        lambda provider: runtime.proof if provider == "finlab" else platform_limits(provider),
    )

    def fetch_dataset(name: str, *, target_date: date, symbols: tuple[str, ...]) -> Any:
        with acquisition.Acquisition("finlab") as operation:
            operation.start()
            return sdk_get(name, target_date=target_date, symbols=symbols)

    def sdk_get(name: str, *, target_date: date, symbols: tuple[str, ...]) -> Any:
        gateway.calls.append((name, clock.elapsed, symbols))
        if len(gateway.calls) == gateway.fail_at:
            # What the real gateway raises for any failed table fetch.
            raise FinLabOperationalError("offline SDK failure", gateway.failure)
        value = 1000 if name == "price:成交股數" else 10
        # The first SDK call includes initialization time; next call must still be paced.
        if len(gateway.calls) == 1:
            clock.elapsed += 2
        return FinLabDatasetTable(
            dates=(target_date.isoformat(),),
            symbols=symbols,
            values=(tuple(value for _ in symbols),),
        )

    gateway.fetch_dataset = fetch_dataset
    runtime.adapter.gateway = lambda *_: gateway
    return runtime, source, raw, clock, SimpleNamespace(gateway=gateway, plan=plan, release=release)


def _quota(runtime: Any, window: str) -> tuple[int, int]:
    with runtime.state.connection() as db:
        row = db.execute(
            "SELECT SUM(requests) AS requests, SUM(bytes) AS bytes FROM full_quota WHERE (account='finlab' OR account LIKE 'finlab:%') AND window=?",
            (window,),
        ).fetchone()
        return (row["requests"] or 0, row["bytes"] or 0) if row else (0, 0)


def test_finlab_two_feeds_share_pacing_and_ten_call_account_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, source, raw, clock, context = _finlab_runtime(tmp_path, monkeypatch, "tw_equity_eod")
    datasets = ["tw_equity_eod", "tw_etf_eod"]
    proof = {**runtime.proof, "datasets": datasets}
    path = tmp_path / "readiness.json"
    path.write_bytes(canonical_bytes(proof))
    runtime.proof = load_readiness(path, "finlab", datasets, now=clock.now())
    requests = []
    for dataset in datasets:
        feed = {
            **runtime.feeds[0],
            "dataset_key": dataset,
            "asset_class": "equity" if dataset == datasets[0] else "etf",
        }
        plan = {**context.plan, "plan_id": "plan:" + dataset, "dataset_key": dataset}
        member = context.release["members"][0]
        args = (member, plan, plan["parts"][0], context.release, feed)
        first = runtime._acquire(*args)
        requests.append(first)
        cached = runtime._acquire(*args)
        assert cached[0]["payload"] == first[0]["payload"]
        assert cached[0]["idempotency_key"] == first[0]["idempotency_key"]
    calls = context.gateway.calls
    assert len(calls) == 10
    assert all(later[1] - earlier[1] >= 1 for earlier, later in zip(calls, calls[1:]))
    assert _quota(runtime, "2026-10-01") == (10, 0)
    assert _quota(runtime, "2026-10-01T12:01") == (10, 0)
    assert len(raw.calls) == 2
    assert [request[0]["dataset_key"] for request in requests] == datasets
    assert source.outcomes == []
    runtime.http.close()


@pytest.mark.parametrize(
    (
        "calendar_repair",
        "created_offset",
        "expected_exception",
        "expected_status",
        "expected_reason",
    ),
    [
        (False, 0, ProviderPublicationPendingError, "pending", "target_not_ready"),
        (True, 0, CalendarDecisionRepairRequiredError, "manual", "calendar_repair_required"),
        (
            True,
            -14401,
            CalendarDecisionRepairRequiredError,
            "manual",
            "calendar_repair_required",
        ),
        (False, -14401, None, "manual", "target_not_ready"),
    ],
)
def test_finlab_target_not_ready_uses_bounded_nonconsuming_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calendar_repair: bool,
    created_offset: int,
    expected_exception: type[Exception] | None,
    expected_status: str,
    expected_reason: str,
) -> None:
    runtime, _, _, clock, context = _finlab_runtime(tmp_path, monkeypatch, "tw_equity_eod")
    member_key = context.plan["parts"][0]["member_keys"][0]
    context.plan["parts"][0]["member_keys"] = [member_key]
    runtime._acquire = lambda *args, **kwargs: (_ for _ in ()).throw(
        FinLabTargetNotReadyError("target date has not been published")
    )
    # The pre-claim round probe sees an open date; the publication wait probes again.
    probes = iter([False, calendar_repair])
    owner = SimpleNamespace(
        work={
            "work_id": "daily-work",
            "kind": "daily",
            "trade_date": context.plan["trade_date"],
            "created_at": (clock.now() + timedelta(seconds=created_offset)).isoformat(),
        },
        request=lambda action: {
            "work_id": "daily-work",
            "trade_date": context.plan["trade_date"],
            "repair_required": next(probes),
        },
        restore=lambda **kwargs: [],
        authorize=lambda: None,
        dispatch_observations=0,
    )
    monkeypatch.setattr("findb_fetcher.provider_work.active", lambda: owner)

    if expected_exception is None:
        assert not runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
    else:
        with pytest.raises(expected_exception):
            runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])

    key = context.plan["parts"][0]["work_item_id"] + ":" + member_key
    with runtime.state.connection() as db:
        row = db.execute(
            "SELECT status,reason,attempts,next_at FROM full_work WHERE key=?", (key,)
        ).fetchone()
    assert (row["status"], row["reason"]) == (expected_status, expected_reason)
    if expected_exception is ProviderPublicationPendingError:
        assert row["attempts"] == 0
        assert row["next_at"] == pytest.approx(clock.now().timestamp() + 900)
    runtime.http.close()


def test_finlab_publication_retry_checks_calendar_before_provider_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, _, clock, context = _finlab_runtime(tmp_path, monkeypatch, "tw_equity_eod")
    member_key = context.plan["parts"][0]["member_keys"][0]
    context.plan["parts"][0]["member_keys"] = [member_key]
    calendar_requests: list[str] = []

    def request(action: str) -> dict[str, object]:
        calendar_requests.append(action)
        # The pre-claim round probe sees an open date; the observed retry probes again.
        return {
            "work_id": "daily-work",
            "trade_date": context.plan["trade_date"],
            "repair_required": len(calendar_requests) > 1,
        }

    journal = WorkJournal(tmp_path / "provider-work.sqlite3", "daily-work")
    journal.observe_response("finlab_dataset:test", b"{}", origin="native")
    owner = SimpleNamespace(
        work={
            "work_id": "daily-work",
            "kind": "daily",
            "trade_date": context.plan["trade_date"],
            "created_at": clock.now().isoformat(),
        },
        journal=journal,
        request=request,
        restore=lambda **kwargs: [],
        authorize=lambda: pytest.fail("calendar repair must precede authorization"),
        dispatch_observations=0,
    )
    runtime._acquire = lambda *args, **kwargs: pytest.fail(
        "calendar repair must precede provider acquisition"
    )
    monkeypatch.setattr("findb_fetcher.provider_work.active", lambda: owner)

    with pytest.raises(CalendarDecisionRepairRequiredError):
        runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])

    key = context.plan["parts"][0]["work_item_id"] + ":" + member_key
    with runtime.state.connection() as db:
        row = db.execute("SELECT status,reason FROM full_work WHERE key=?", (key,)).fetchone()
    assert (row["status"], row["reason"]) == ("manual", "calendar_repair_required")
    assert calendar_requests == ["calendar-status", "calendar-status"]
    runtime.http.close()


@pytest.mark.parametrize("dataset", ["tw_equity_eod", "tw_etf_eod"])
def test_finlab_field_calls_paced_cached_and_prepared_identity_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dataset: str
) -> None:
    runtime, source, raw, clock, context = _finlab_runtime(tmp_path, monkeypatch, dataset)
    contracts = ContractRegistry(Path(__file__).resolve().parents[2] / "contracts")
    preparer = SourceAPIClient(
        FetcherConfig("https://source.example", "offline"), contracts, client=runtime.http
    )
    delivered: list[bytes] = []
    observed: list[bytes] = []

    def prepare(request: dict[str, Any]) -> Any:
        assert len(raw.calls) == 1  # Entire bundle is persisted before any Source preparation.
        assert request["payload"]["batch"]["source_raw_sha256"] == checksum(raw.calls[0])
        key = dataset + ":" + request["payload"]["data"][0]["symbol"]
        assert runtime.state.prepared(key) == canonical_bytes([request])
        prepared = preparer.prepare(request)  # Validate against the real ingress contract.
        observed.append(prepared.body)
        return prepared

    runtime.delivery = SimpleNamespace(
        prepare=prepare,
        deliver=lambda body: delivered.append(body.body) or SimpleNamespace(run_id=UUID(int=1)),
        get_run_status=lambda *args, **kwargs: SimpleNamespace(
            status="processing", failed_records=0
        ),
    )
    assert runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
    calls = context.gateway.calls
    assert len(calls) == 5
    assert [later[1] - earlier[1] for earlier, later in zip(calls, calls[1:])] == [3, 1, 1, 1]
    assert all(call[2] == tuple(context.plan["parts"][0]["member_keys"]) for call in calls)
    assert _quota(runtime, "2026-10-01") == (5, 0)
    assert _quota(runtime, "2026-10-01T12:01") == (5, 0)
    assert len(delivered) == 2
    saved = {
        key: runtime.state.prepared(key)
        for key in (dataset + ":" + symbol for symbol in context.plan["parts"][0]["member_keys"])
    }
    runtime.adapter.bundles.clear()
    runtime.state = FullMarketState(runtime.state.path)
    clock.elapsed += 61
    runtime.delivery.get_run_status = lambda *args, **kwargs: SimpleNamespace(
        status="completed", failed_records=0, total_records=1, success_records=1
    )
    assert runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
    assert len(context.gateway.calls) == 5
    assert len(raw.calls) == 1
    assert len(delivered) == 2
    # Accepted bodies only read their runs after restart: nothing is prepared
    # again, and the restored bodies keep the delivered byte identity.
    assert len(observed) == 2 and delivered == observed
    assert saved == {key: runtime.state.prepared(key) for key in saved}
    assert sorted(
        delivered_snapshot(request).body for body in saved.values() for request in json.loads(body)
    ) == sorted(observed)
    assert source.outcomes == []
    runtime.http.close()


@pytest.mark.parametrize("dataset", ["tw_equity_eod", "tw_etf_eod"])
@pytest.mark.parametrize("boundary", ["daily", "minute", "failure", "stop"])
def test_finlab_partial_bundle_remains_durable_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dataset: str, boundary: str
) -> None:
    runtime, source, raw, clock, context = _finlab_runtime(tmp_path, monkeypatch, dataset)
    # One member suffices to prove failure is durable without triggering a separate retry.
    context.plan["parts"][0]["member_keys"] = context.plan["parts"][0]["member_keys"][:1]
    if boundary in {"daily", "minute"}:
        runtime.proof["requests_per_day" if boundary == "daily" else "requests_per_minute"] = 2
    elif boundary == "failure":
        context.gateway.fail_at = 3
    else:
        runtime.can_acquire = lambda: clock.elapsed < 103.5
    runtime.delivery = SimpleNamespace(
        prepare=lambda *args: pytest.fail("partial bundle delivered")
    )
    if boundary == "failure":
        # A failed table fetch holds the whole plan; no member answers for it.
        with pytest.raises(ProviderOutagePendingError) as pending:
            runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
        assert pending.value.retry_after_seconds == 300
    else:
        runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
    expected_calls = 3 if boundary == "failure" else 2
    assert len(context.gateway.calls) == expected_calls
    assert (dataset, date(2026, 10, 1)) not in runtime.adapter.bundles
    assert raw.calls == []
    key = dataset + ":" + context.plan["parts"][0]["member_keys"][0]
    assert runtime.state.prepared(key) is None
    runtime.state = FullMarketState(runtime.state.path)
    with runtime.state.connection() as db:
        row = db.execute("SELECT status, reason FROM full_work WHERE key=?", (key,)).fetchone()
        assert row["status"] == "pending"
        assert row["reason"] == (
            "rate_limited"
            if boundary in {"daily", "minute"}
            else "suspended"
            if boundary == "stop"
            else "provider_unavailable"
        )
    # Pre-start minute rejection creates no known request debt.
    assert _quota(runtime, "2026-10-01")[0] == (3 if boundary == "failure" else 2)
    assert _quota(runtime, "2026-10-01T12:01")[0] == expected_calls
    assert source.outcomes == []
    assert all(wait <= 1 for wait in clock.waits)
    runtime.http.close()


def _work_rows(runtime: Any, plan: dict) -> dict[str, tuple[str, str | None, int]]:
    with runtime.state.connection() as db:
        return {
            row["member_key"]: (row["status"], row["reason"], row["attempts"])
            for row in db.execute(
                "SELECT member_key,status,reason,attempts FROM full_work WHERE plan_id=?",
                (plan["plan_id"],),
            )
        }


def test_finlab_table_failure_holds_the_plan_without_charging_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, source, raw, clock, context = _finlab_runtime(tmp_path, monkeypatch, "tw_equity_eod")
    first, second = context.plan["parts"][0]["member_keys"]
    context.gateway.fail_at = 1
    runtime.delivery = SimpleNamespace(prepare=lambda *args: pytest.fail("delivered"))

    with pytest.raises(ProviderOutagePendingError) as pending:
        runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])

    assert str(pending.value) == "finlab_network" and pending.value.retry_after_seconds == 300
    # Only the member being read is deferred, and no attempt is charged to anyone.
    assert _work_rows(runtime, context.plan) == {
        first: ("pending", "provider_unavailable", 0),
        second: ("pending", None, 0),
    }
    assert source.outcomes == [] and raw.calls == []
    with runtime.state.connection() as db:
        assert [row[0] for row in db.execute("SELECT category FROM full_work_diagnostic")] == [
            "finlab_sdk:network"
        ]

    # The quota does not come back in minutes: the plan waits longer.
    clock.elapsed += 400
    context.gateway.fail_at, context.gateway.failure = len(context.gateway.calls) + 1, "quota"
    with pytest.raises(ProviderOutagePendingError) as pending:
        runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
    assert str(pending.value) == "finlab_quota" and pending.value.retry_after_seconds == 1800

    # Past the outage window the work ends; members are still not charged one by one.
    clock.elapsed += 4 * 3600
    context.gateway.fail_at, context.gateway.failure = len(context.gateway.calls) + 1, "network"
    with pytest.raises(ProviderWorkRejectedError) as rejected:
        runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
    assert rejected.value.code == "DAILY_PROVIDER_OUTAGE_EXHAUSTED"
    assert source.outcomes == []
    assert {row[2] for row in _work_rows(runtime, context.plan).values()} == {0}
    assert not runtime.state.has_blocking_manual(context.plan["plan_id"])
    runtime.http.close()


def test_finlab_rejected_credential_ends_the_work_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, source, raw, clock, context = _finlab_runtime(tmp_path, monkeypatch, "tw_etf_eod")
    context.gateway.fail_at, context.gateway.failure = 1, "auth"

    with pytest.raises(ProviderWorkRejectedError) as rejected:
        runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])

    assert rejected.value.code == "DAILY_PROVIDER_REJECTED" and str(rejected.value) == "finlab_auth"
    assert len(context.gateway.calls) == 1 and source.outcomes == []
    # The claimed member is released without a charge; the other was never read.
    assert set(_work_rows(runtime, context.plan).values()) == {
        ("pending", None, 0),
        ("pending", "provider_rejected", 0),
    }
    with runtime.state.connection() as db:
        assert [row[0] for row in db.execute("SELECT category FROM full_work_diagnostic")] == [
            "finlab_sdk:auth"
        ]
        assert db.execute("SELECT COUNT(*) FROM full_provider_outage").fetchone()[0] == 0
    runtime.http.close()


def test_a_successful_fetch_ends_the_finlab_outage_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, source, raw, clock, context = _finlab_runtime(tmp_path, monkeypatch, "tw_equity_eod")
    context.gateway.fail_at = 1
    runtime.delivery = SimpleNamespace(
        prepare=lambda request: SimpleNamespace(body=canonical_bytes(request), request=request),
        deliver=lambda body: SimpleNamespace(run_id=UUID(int=1)),
        get_run_status=lambda *args, **kwargs: SimpleNamespace(
            status="completed", failed_records=0, total_records=1, success_records=1
        ),
    )
    with pytest.raises(ProviderOutagePendingError):
        runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
    with runtime.state.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_provider_outage").fetchone()[0] == 1

    clock.elapsed += 400
    context.gateway.fail_at = None
    runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])

    with runtime.state.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_provider_outage").fetchone()[0] == 0
    assert {row[0] for row in _work_rows(runtime, context.plan).values()} <= {
        "complete",
        "prepared",
    }
    runtime.http.close()


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "taifex"])
def test_slow_verified_rate_is_fully_waited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    runtime, _, _ = _runtime(tmp_path, provider=provider)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    runtime.proof["requests_per_second"] = 1 / 120
    _checkpoint_reserve(runtime)
    _checkpoint_reserve(runtime)
    assert clock.elapsed == 220
    assert clock.waits == [1] * 120
    with runtime.state.connection() as db:
        assert (
            db.execute("SELECT next_at FROM full_pacing").fetchone()[0]
            == clock.origin.timestamp() + 340
        )
    runtime.http.close()


def test_readiness_expiring_during_pacing_does_not_gate_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, _ = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    runtime.proof.update(
        requests_per_second=0.1,
        expires_at=(clock.now() + timedelta(seconds=2)).isoformat(),
    )
    _checkpoint_reserve(runtime)
    _checkpoint_reserve(runtime)
    assert clock.elapsed == 110
    with runtime.state.connection() as db:
        assert db.execute("SELECT SUM(requests) FROM full_quota").fetchone()[0] == 4
    runtime.http.close()


@pytest.mark.parametrize("dataset", ["tw_equity_eod", "tw_etf_eod"])
def test_finlab_member_failures_stay_member_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dataset: str
) -> None:
    runtime, source, raw, clock, context = _finlab_runtime(tmp_path, monkeypatch, dataset)
    nan = float("nan")
    observations = {
        "2330": {"open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000},
        # A listed symbol without trades that day: every price empty, no volume.
        "1538": {"open": nan, "high": nan, "low": nan, "close": nan, "volume": nan},
        "1589": {"open": 10, "high": 9, "low": 8, "close": 10, "volume": 1000},
        "4804": {"open": 10, "high": nan, "low": 9, "close": 10, "volume": 1000},
        # Shares changed hands without a regular-session price: an earlier close exists.
        "3085": {"open": nan, "high": nan, "low": nan, "close": nan, "volume": 42},
        # The same pattern for a symbol FinLab has never priced: nothing to carry.
        "7770": {"open": nan, "high": nan, "low": nan, "close": nan, "volume": 1},
    }
    earlier_close = (("1538", "2026-09-30", 7.24), ("3085", "2026-09-29", 25.4))
    fields = {
        "price:開盤價": "open",
        "price:最高價": "high",
        "price:最低價": "low",
        "price:收盤價": "close",
        "price:成交股數": "volume",
    }
    served = tuple(observations)
    members = [*served, "00416A", "00418A"]
    context.release["members"] = [
        {
            "symbol": symbol,
            "provider_symbol": symbol,
            "member_key": symbol,
            "mapping_status": "gap" if symbol == "00418A" else "mapped",
        }
        for symbol in members
    ]
    context.plan["parts"][0]["member_keys"] = members
    runtime.state.record_plan(context.plan, "finlab")
    from findb_fetcher import acquisition

    def fetch_dataset(name: str, *, target_date: date, symbols: tuple[str, ...]) -> Any:
        with acquisition.Acquisition("finlab") as operation:
            operation.start()
            context.gateway.calls.append((name, clock.elapsed, symbols))
            # FinLab does not serve 00416A yet; the gateway narrows to served symbols.
            present = tuple(symbol for symbol in symbols if symbol in observations)
            return FinLabDatasetTable(
                dates=(target_date.isoformat(),),
                symbols=present,
                values=(tuple(observations[symbol][fields[name]] for symbol in present),),
                previous=earlier_close if name == "price:收盤價" else (),
            )

    context.gateway.fetch_dataset = fetch_dataset
    delivered: list[Any] = []
    runtime.delivery = SimpleNamespace(
        prepare=lambda request: SimpleNamespace(body=canonical_bytes(request), request=request),
        deliver=lambda body: delivered.append(body.request) or SimpleNamespace(run_id=UUID(int=1)),
        get_run_status=lambda *args, **kwargs: SimpleNamespace(
            status="completed", failed_records=0, total_records=1, success_records=1
        ),
    )

    runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])

    calls = context.gateway.calls
    assert len(calls) == 5
    assert all(call[2] == (*served, "00416A") for call in calls)
    rows = {
        request["payload"]["data"][0]["symbol"]: request["payload"]["data"][0]
        for request in delivered
    }
    assert list(rows) == ["2330", "3085"]
    assert {field: rows["3085"][field] for field in ("open", "high", "low", "close", "volume")} == {
        "open": "25.4",
        "high": "25.4",
        "low": "25.4",
        "close": "25.4",
        "volume": 42,
    }
    # The shared raw keeps the empty cells and where each carried close came from.
    shared = json.loads(raw.calls[0])
    assert shared["close"]["previous"] == [
        ["1538", "2026-09-30", "7.24"],
        ["3085", "2026-09-29", "25.4"],
    ]
    assert shared["close"]["values"][0][shared["close"]["symbols"].index("3085")] == "nan"
    assert "previous" not in shared["open"]
    outcomes = {item["member_key"]: item for item in source.outcomes}
    assert {key: item["reason"] for key, item in outcomes.items()} == {
        "1538": "no_trade",
        "1589": "source_error",
        "4804": "source_error",
        "7770": "source_error",
        "00416A": "empty_response",
        "00418A": "mapping_gap",
    }
    evidence = outcomes["1538"]["evidence"]
    assert evidence["source_status"] == "no_trade"
    assert evidence["source_symbol"] == "1538"
    assert evidence["trade_date"] == "2026-10-01"
    assert evidence["sha256"] == checksum(raw.calls[0])
    assert evidence["url"].startswith("https://")
    with runtime.state.connection() as db:
        rows = {
            row["member_key"]: (row["status"], row["reason"])
            for row in db.execute(
                "SELECT member_key,status,reason FROM full_work WHERE plan_id=?",
                (context.plan["plan_id"],),
            )
        }
        diagnostics = sorted(
            row["category"] for row in db.execute("SELECT category FROM full_work_diagnostic")
        )
    assert {key: rows[key] for key in members} == {
        "2330": ("complete", None),
        "1538": ("complete", "source_proven_no_trade"),
        "1589": ("manual", "member_unavailable"),
        "4804": ("manual", "member_unavailable"),
        "3085": ("complete", None),
        "7770": ("manual", "member_unavailable"),
        "00416A": ("manual", "member_unavailable"),
        "00418A": ("manual", "mapping_gap"),
    }
    assert diagnostics == [
        "member:missing_symbol",
        "member:non_finite",
        "member:non_finite",
        "member:ohlc_bounds",
    ]
    assert not runtime.state.has_blocking_manual(context.plan["plan_id"])
    runtime.http.close()
