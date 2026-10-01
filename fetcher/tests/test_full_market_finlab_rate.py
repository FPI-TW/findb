"""Actual FinLab acquisition pacing, durable quotas and prepared-body recovery."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import findb_fetcher.full_market_runtime as runtime_module
from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_runtime import load_readiness, validate_capacity
from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.full_market_universe import canonical_bytes, checksum
from findb_fetcher.providers.finlab import FinLabDatasetTable
from test_full_market import _proof, _runtime


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
    validate_capacity(runtime.proof, 5, seconds_available=60)
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
    gateway = SimpleNamespace(calls=[], fail_at=None)

    def fetch_dataset(name: str, *, target_date: date, symbols: tuple[str, ...]) -> Any:
        gateway.calls.append((name, clock.elapsed, symbols))
        if len(gateway.calls) == gateway.fail_at:
            raise RuntimeError("offline SDK failure")
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
    runtime._finlab_gateway = lambda: gateway
    return runtime, source, raw, clock, SimpleNamespace(gateway=gateway, plan=plan, release=release)


def _quota(runtime: Any, window: str) -> tuple[int, int]:
    with runtime.state.connection() as db:
        row = db.execute(
            "SELECT requests, bytes FROM full_quota WHERE account='finlab' AND window=?",
            (window,),
        ).fetchone()
        return (row["requests"], row["bytes"]) if row else (0, 0)


def test_finlab_two_feeds_share_pacing_and_ten_call_account_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, source, raw, clock, context = _finlab_runtime(tmp_path, monkeypatch, "tw_equity_eod")
    datasets = ["tw_equity_eod", "tw_etf_eod"]
    proof = {**runtime.proof, "datasets": datasets}
    path = tmp_path / "readiness.json"
    path.write_bytes(canonical_bytes(proof))
    runtime.proof = load_readiness(path, "finlab", datasets, now=clock.now())
    validate_capacity(runtime.proof, 10, seconds_available=14400)
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
    assert _quota(runtime, "2026-10-01") == (10, 10000)
    assert _quota(runtime, "2026-10-01T12:01") == (10, 0)
    assert len(raw.calls) == 2
    assert [request[0]["dataset_key"] for request in requests] == datasets
    assert source.outcomes == []
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
    assert _quota(runtime, "2026-10-01") == (5, 5000)
    assert _quota(runtime, "2026-10-01T12:01") == (5, 0)
    assert len(delivered) == 2
    saved = {
        key: runtime.state.prepared(key)
        for key in (dataset + ":" + symbol for symbol in context.plan["parts"][0]["member_keys"])
    }
    runtime.bundles.clear()
    runtime.state = FullMarketState(runtime.state.path)
    clock.elapsed += 61
    runtime.delivery.get_run_status = lambda *args, **kwargs: SimpleNamespace(
        status="completed", failed_records=0
    )
    assert runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
    assert len(context.gateway.calls) == 5
    assert len(raw.calls) == 1
    assert len(delivered) == 2
    assert observed[:2] == observed[2:]
    assert saved == {key: runtime.state.prepared(key) for key in saved}
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
    runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
    expected_calls = 3 if boundary == "failure" else 2
    assert len(context.gateway.calls) == expected_calls
    assert (dataset, date(2026, 10, 1)) not in runtime.bundles
    assert raw.calls == []
    key = dataset + ":" + context.plan["parts"][0]["member_keys"][0]
    assert runtime.state.prepared(key) is None
    runtime.state = FullMarketState(runtime.state.path)
    with runtime.state.connection() as db:
        row = db.execute("SELECT status, reason FROM full_work WHERE key=?", (key,)).fetchone()
        assert row["status"] == "pending"
        assert row["reason"] == (
            "rate_limited" if boundary in {"daily", "minute"} else "source_error"
        )
    # Minute rejection intentionally retains the conservative daily reservation.
    assert _quota(runtime, "2026-10-01")[0] == (3 if boundary in {"minute", "failure"} else 2)
    assert _quota(runtime, "2026-10-01T12:01")[0] == expected_calls
    assert source.outcomes[-1]["reason"] != "no_trade"
    assert all(wait <= 1 for wait in clock.waits)
    runtime.http.close()


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
def test_slow_verified_rate_is_fully_waited_and_stop_rechecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    runtime, _, _ = _runtime(tmp_path, provider=provider)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    runtime.proof["requests_per_second"] = 1 / 120
    runtime._reserve()
    runtime._reserve()
    assert clock.elapsed == 220
    assert clock.waits == [1] * 120
    runtime.can_acquire = lambda: clock.elapsed < 223
    with pytest.raises(runtime_module.ReadinessBlockedError, match="stopped"):
        runtime._reserve()
    assert clock.elapsed == 223
    with runtime.state.connection() as db:
        assert (
            db.execute("SELECT next_at FROM full_pacing").fetchone()[0]
            == clock.origin.timestamp() + 340
        )
    runtime.http.close()


def test_readiness_expiring_during_pacing_does_not_reserve_or_acquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, _ = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    runtime.proof.update(
        requests_per_second=0.1,
        expires_at=(clock.now() + timedelta(seconds=2)).isoformat(),
    )
    runtime._reserve()
    with pytest.raises(runtime_module.ReadinessBlockedError, match="expired"):
        runtime._reserve()
    assert clock.elapsed == 102
    with runtime.state.connection() as db:
        assert db.execute("SELECT SUM(requests) FROM full_quota").fetchone()[0] == 2
    runtime.http.close()
