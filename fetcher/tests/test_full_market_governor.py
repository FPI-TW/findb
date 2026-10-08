"""Real acquisition boundaries and shared durable account pacing regressions."""

from __future__ import annotations

import gzip
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import findb_fetcher.full_market_runtime as runtime_module
from findb_fetcher.full_market_runtime import ReadinessBlockedError
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.full_market_universe import TAIFEX_URL, UniverseSnapshotError, canonical_bytes
from findb_fetcher.providers.finlab import FinLabSdkGateway
from findb_fetcher.providers.shioaji import ShioajiKbarsSnapshot
from findb_fetcher.providers.taifex import fetch_report
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataResponseError,
)
from test_full_market import _member, _plan, _proof, _runtime
from test_full_market_finlab_rate import _Clock, _finlab_runtime, _quota


@pytest.mark.parametrize("path", ["catalogue", "daily"])
@pytest.mark.parametrize("response_crosses_midnight", [False, True])
def test_twelve_data_429_account_cooldown_crosses_day_and_shared_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    response_crosses_midnight: bool,
) -> None:
    first, source, raw = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    clock.elapsed = 43199
    first.proof = _proof(clock.now())
    first._ensure_proof = lambda: None
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-secret")
    calls = []

    def limited(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, clock.now()))
        if response_crosses_midnight:
            clock.elapsed += 2
        return httpx.Response(429, json={"status": "error", "code": 429})

    with httpx.Client(transport=httpx.MockTransport(limited)) as http:
        if path == "catalogue":
            first.http.close()
            first.http = http
            with pytest.raises(QuotaBlockedError):
                first._catalogue()
        else:
            first.td = TwelveDataClient(TwelveDataConfig(api_key="offline-secret"), client=http)
            plan = _plan()
            plan["parts"][0]["member_keys"] = ["AAPL"]
            first.state.record_plan(plan, first.provider)
            assert not first._deliver_plan(
                plan, {"release_id": "r", "members": [_member()]}, first.feeds[0]
            )
            assert source.outcomes[-1]["reason"] == "rate_limited"
    cooldown_until = clock.now().timestamp() + 60
    with first.state.connection() as db:
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] == cooldown_until
        assert db.execute(
            "SELECT requests,bytes FROM full_quota WHERE window='2026-10-01'"
        ).fetchone()[:] == (1, 1000)
        assert db.execute("SELECT * FROM full_quota WHERE window='2026-10-02'").fetchone() is None
    assert raw.calls == [] and first.catalogue_evidence == {}
    # Cross midnight before a second HK runtime opens the same account state.
    clock.elapsed = max(clock.elapsed, 43201)
    second, _, _ = _runtime(tmp_path)
    second.proof = first.proof.copy()
    second.feeds[0]["dataset_key"] = "hk_equity_eod"
    second.proof["expires_at"] = (clock.now() + timedelta(seconds=2)).isoformat()
    with pytest.raises(ReadinessBlockedError, match="expired"):
        second._reserve()
    with second.state.connection() as db:
        assert db.execute("SELECT * FROM full_quota WHERE window='2026-10-02'").fetchone() is None
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] == cooldown_until
    second.proof = _proof(clock.now())

    def acquired(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, clock.now()))
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(acquired)) as http:
        second.td = TwelveDataClient(TwelveDataConfig(api_key="offline-secret"), client=http)
        hk_plan = {**_plan(), "dataset_key": "hk_equity_eod"}
        with pytest.raises(TwelveDataResponseError):
            second._acquire(
                _member(), hk_plan, hk_plan["parts"][0], {"release_id": "r"}, second.feeds[0]
            )
    assert len(calls) == 2 and calls[-1][1].timestamp() == cooldown_until
    assert all(wait <= 1 for wait in clock.waits)
    with second.state.connection() as db:
        assert db.execute(
            "SELECT requests,bytes FROM full_quota WHERE window='2026-10-02'"
        ).fetchone()[:] == (1, 1000)
    first.http.close()
    second.http.close()


def test_legacy_dated_cooldown_promoted_and_new_cooldown_preserves_later_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, _ = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    clock.elapsed = 43201
    runtime.proof = _proof(clock.now())
    until = clock.now().timestamp() + 5
    with runtime.state.connection() as db:
        db.execute("INSERT INTO full_quota VALUES('twelve_data','2026-10-01',1,1000,?)", (until,))
    runtime.state = FullMarketState(runtime.state.path)
    runtime.state.rate_limited(account="twelve_data", window="2026-10-02", until=until - 2)
    runtime._reserve()
    assert clock.now().timestamp() == until and clock.waits == [1] * 5
    assert _daily(runtime) == (1, 1000)
    with runtime.state.connection() as db:
        assert db.execute(
            "SELECT requests,bytes FROM full_quota WHERE window='2026-10-02'"
        ).fetchone()[:] == (1, 1000)
    runtime.http.close()


@pytest.mark.parametrize("fail_at", [None, 1, 2])
def test_finlab_catalogue_slow_gateway_completion_paces_next_field_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int | None
) -> None:
    runtime, source, raw, clock, _ = _finlab_runtime(tmp_path, monkeypatch, "tw_equity_eod")
    runtime._ensure_proof = lambda: None
    runtime.feeds[0]["source_timezone"] = "Asia/Taipei"
    runtime.feeds.append({**runtime.feeds[0], "dataset_key": "tw_etf_eod", "asset_class": "etf"})
    runtime.installed_feeds = list(runtime.feeds)
    runtime.proof["datasets"] = ["tw_equity_eod", "tw_etf_eod"]
    members = {
        "equity": {
            **_member(),
            "symbol": "2330",
            "provider_symbol": "2330",
            "exchange": "XTAI",
            "classification": "ordinary",
        },
        "etf": {
            **_member(),
            "symbol": "0050",
            "provider_symbol": "0050",
            "exchange": "XTAI",
            "classification": "etf",
        },
    }
    monkeypatch.setattr(
        runtime_module,
        "parse_tw_companies",
        lambda body, *, otc: [] if otc else [members["equity"]],
    )
    monkeypatch.setattr(
        runtime_module,
        "parse_tw_ordinary",
        lambda body, *, otc: [] if otc else [members["equity"].copy()],
    )
    monkeypatch.setattr(
        runtime_module, "parse_tw_etfs", lambda body, *, otc: [] if otc else [members["etf"].copy()]
    )
    runtime._snapshot = lambda url: b"official snapshot"
    submitted = []
    source.submit = lambda body: submitted.append(body) or body
    calls = []
    frame = SimpleNamespace(
        index=(clock.now().astimezone(runtime_module.ZoneInfo("Asia/Taipei")).date(),),
        columns=("2330", "0050"),
        loc=_Loc(),
    )

    def get(name: str) -> Any:
        calls.append((name, clock.elapsed))
        clock.elapsed += 2
        if len(calls) == fail_at:
            raise RuntimeError("offline SDK failure")
        return frame

    gateway = FinLabSdkGateway(
        api_token="offline-secret",
        _sdk=SimpleNamespace(login=lambda token: None, data=SimpleNamespace(get=get)),
    )
    runtime._finlab_gateway = lambda: gateway
    runtime.sync_universes()
    assert [call[1] for call in calls] == ([100] if fail_at == 1 else [100, 103])
    assert len(submitted) == 2 and runtime.bundles == {}
    assert _quota(runtime, "2026-10-01") == (len(calls), len(calls) * 1000)
    completion = clock.elapsed
    fresh, _, _ = _runtime(tmp_path, provider="finlab")
    fresh.proof = runtime.proof.copy()
    fresh._reserve()
    assert clock.elapsed == completion + 1
    assert _quota(fresh, "2026-10-01") == (len(calls) + 1, (len(calls) + 1) * 1000)
    assert sum(b"dates" in body for body in raw.calls) == (
        0 if fail_at == 1 else 1 if fail_at == 2 else 2
    )
    runtime.http.close()
    fresh.http.close()


class _Body(httpx.SyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __iter__(self) -> Any:
        yield self.body


@pytest.mark.parametrize("path", ["catalogue", "daily"])
@pytest.mark.parametrize("status", [200, 429])
@pytest.mark.parametrize(
    "boundary",
    ["declared", "negative_length", "wire", "encoding", "transport", "non_json", "shape"],
)
def test_known_http_429_survives_invalid_body_and_fresh_shared_hk_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    status: int,
    boundary: str,
) -> None:
    first, _, raw = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    first.proof = _proof(clock.now())
    first._ensure_proof = lambda: None
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-secret")
    headers = {}
    body = b"[]"
    if boundary == "declared":
        headers = {"Content-Length": "1001"}
    elif boundary == "negative_length":
        headers = {"Content-Length": "-1"}
    elif boundary == "wire":
        body = b"x" * 1200
    elif boundary == "encoding":
        headers = {"Content-Encoding": "br"}
    elif boundary == "non_json":
        body = b"not JSON"
    calls = []
    started_at = clock.now().timestamp()

    class CheckedBody(_Body):
        def __iter__(self) -> Any:
            if status == 429:
                with first.state.connection() as db:
                    assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] == (
                        started_at + 60
                    )
            if boundary == "transport":
                raise httpx.ReadError("offline read failure")
            yield from super().__iter__()

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(status, headers=headers, stream=CheckedBody(body))

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        if path == "catalogue":
            first.http.close()
            first.http = http
            error = (
                QuotaBlockedError
                if status == 429
                else ReadinessBlockedError
                if boundary in {"declared", "negative_length", "wire", "encoding", "shape"}
                else httpx.ReadError
                if boundary == "transport"
                else json.JSONDecodeError
            )
            with pytest.raises(error):
                first._catalogue()
        else:
            first.td = TwelveDataClient(TwelveDataConfig(api_key="offline-secret"), client=http)
            with pytest.raises(TwelveDataResponseError) as caught:
                first._acquire(
                    _member(), _plan(), _plan()["parts"][0], {"release_id": "r"}, first.feeds[0]
                )
            assert caught.value.status_code == status
            assert caught.value.is_rate_limited is (status == 429)
            assert caught.value.observed_bytes == (
                1200 if boundary == "wire" else 2 if boundary == "shape" and status == 429 else 0
            )
    assert len(calls) == 1
    assert _daily(first) == (1, 1200 if boundary == "wire" else 1000)
    assert raw.calls == [] and first.catalogue_evidence == {} and first.bundles == {}
    assert first.state.prepared("fp1:123:AAPL") is None
    with first.state.connection() as db:
        permit = db.execute("SELECT next_at FROM full_pacing").fetchone()[0]
        assert permit == started_at + (60 if status == 429 else 0.1)
    # A fresh HK runtime must respect the remaining account cooldown after restart.
    clock.elapsed += 1
    second, _, _ = _runtime(tmp_path)
    second.proof = first.proof.copy()
    second.feeds[0]["dataset_key"] = "hk_equity_eod"
    if boundary in {"declared", "wire"}:
        with pytest.raises(QuotaBlockedError, match="capacity violation"):
            second._reserve()
        assert clock.now().timestamp() == started_at + 1
        assert clock.waits == []
        assert _daily(second) == (1, 1200 if boundary == "wire" else 1000)
    else:
        second._reserve()
        assert clock.now().timestamp() == started_at + (60 if status == 429 else 1)
        assert clock.waits == ([1] * 59 if status == 429 else [])
        assert _daily(second) == (2, 2000)
    first.http.close()
    second.http.close()


@pytest.mark.parametrize("path", ["catalogue", "daily"])
def test_http_429_cooldown_commits_before_secondary_byte_ledger_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    runtime, _, raw = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    runtime._ensure_proof = lambda: None
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-secret")
    original_overage = runtime.state.observe_capacity
    until = clock.now().timestamp() + 60

    def record_then_fail(**kwargs: Any) -> None:
        with runtime.state.connection() as db:
            assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] == until
        original_overage(**kwargs)
        raise QuotaBlockedError("secondary ledger failure")

    monkeypatch.setattr(runtime.state, "observe_capacity", record_then_fail)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, stream=_Body(b"x" * 1200))
        )
    ) as http:
        if path == "catalogue":
            runtime.http.close()
            runtime.http = http
            acquire = runtime._catalogue
        else:
            runtime.td = TwelveDataClient(TwelveDataConfig(api_key="offline-secret"), client=http)

            def acquire() -> Any:
                return runtime._acquire(
                    _member(), _plan(), _plan()["parts"][0], {"release_id": "r"}, runtime.feeds[0]
                )

        with pytest.raises(QuotaBlockedError, match="secondary ledger failure"):
            acquire()
    assert _daily(runtime) == (1, 1200)
    assert raw.calls == [] and runtime.catalogue_evidence == {}
    clock.elapsed += 1
    fresh, _, _ = _runtime(tmp_path)
    fresh.proof = runtime.proof.copy()
    with pytest.raises(QuotaBlockedError, match="capacity violation"):
        fresh._reserve()
    assert clock.now().timestamp() == until - 59 and clock.waits == []
    with fresh.state.connection() as db:
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] == until
    runtime.http.close()
    fresh.http.close()


@pytest.mark.parametrize("body", [b"not JSON", b"x" * 1200])
def test_daily_invalid_http_429_stops_delivery_without_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    runtime, source, raw = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL"]
    runtime.state.record_plan(plan, runtime.provider)
    runtime.delivery = SimpleNamespace(
        prepare=lambda *args: pytest.fail("invalid 429 response prepared")
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(429, stream=_Body(body)))
    ) as http:
        runtime.td = TwelveDataClient(TwelveDataConfig(api_key="offline-secret"), client=http)
        assert not runtime._deliver_plan(
            plan, {"release_id": "r", "members": [_member()]}, runtime.feeds[0]
        )
    assert source.outcomes[-1]["reason"] == "rate_limited"
    assert runtime.state.prepared("fp1:123:AAPL") is None and raw.calls == []
    assert _daily(runtime) == (1, max(1000, len(body)))
    runtime.http.close()


def _daily(runtime: Any) -> tuple[int, int]:
    with runtime.state.connection() as db:
        row = db.execute(
            "SELECT requests,bytes FROM full_quota WHERE account=? AND length(window)=10",
            (runtime.provider,),
        ).fetchone()
        return row["requests"], row["bytes"]


@pytest.mark.parametrize("declared_length", [False, True])
def test_actual_twelve_data_stream_uses_readiness_cap_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_length: bool,
) -> None:
    runtime, _, raw = _runtime(tmp_path)
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-secret")
    body = canonical_bytes({"status": "ok", "meta": {}, "padding": "x" * 2228})
    assert len(body) == 2266
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(body))} if declared_length else {},
            stream=_Body(body),
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        runtime.td = TwelveDataClient(
            TwelveDataConfig(api_key="offline-secret", max_response_bytes=8000000), client=client
        )
        plan = _plan()
        plan["parts"][0]["member_keys"] = ["AAPL"]
        runtime.state.record_plan(plan, runtime.provider)
        runtime.delivery = SimpleNamespace(
            prepare=lambda *args: pytest.fail("oversized provider response prepared")
        )
        runtime._deliver_plan(plan, {"release_id": "r", "members": [_member()]}, runtime.feeds[0])
    assert len(calls) == 1
    assert raw.calls == []
    assert runtime.state.prepared("fp1:123:AAPL") is None
    # Declared length rejects before reading; a received oversized chunk is retained in ledger.
    assert _daily(runtime) == (1, 1000 if declared_length else len(body))
    runtime.http.close()


def test_actual_twelve_data_creation_tightens_environment_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, _ = _runtime(tmp_path)
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-secret")
    monkeypatch.setenv("TWELVE_DATA_MAX_RESPONSE_BYTES", "8000000")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: runtime.http)
    with pytest.raises(TwelveDataResponseError):
        runtime._acquire(
            _member(), _plan(), _plan()["parts"][0], {"release_id": "r"}, runtime.feeds[0]
        )
    assert runtime.td._config.max_response_bytes == 1000
    runtime.http.close()


class _Loc:
    def __getitem__(self, key: Any) -> int:
        return 1000 if self.volume else 10

    volume = False


def _real_finlab_gateway(runtime: Any, symbols: tuple[str, ...]) -> list[str]:
    calls = []
    loc = _Loc()
    frame = SimpleNamespace(index=(date(2026, 10, 1),), columns=symbols, loc=loc)

    def get(name: str) -> Any:
        calls.append(name)
        loc.volume = name == "price:成交股數"
        return frame

    gateway = FinLabSdkGateway(
        api_token="offline-secret",
        _sdk=SimpleNamespace(login=lambda token: None, data=SimpleNamespace(get=get)),
    )
    runtime._finlab_gateway = lambda: gateway
    return calls


@pytest.mark.parametrize("bound,expected_calls,expected_bytes", [(1000, 1, 2449), (2500, 5, 12849)])
def test_real_finlab_gateway_oversized_field_rejects_and_records_observable_overage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound: int,
    expected_calls: int,
    expected_bytes: int,
) -> None:
    runtime, source, raw, _, context = _finlab_runtime(tmp_path, monkeypatch, "tw_equity_eod")
    symbols = tuple(f"{1000 + index}" for index in range(200))
    context.release["members"] = [
        {
            "symbol": symbol,
            "provider_symbol": symbol,
            "member_key": symbol,
            "mapping_status": "mapped",
        }
        for symbol in symbols
    ]
    context.plan["parts"][0]["member_keys"] = [symbols[0]]
    runtime.state.record_plan(context.plan, runtime.provider)
    calls = _real_finlab_gateway(runtime, symbols)
    runtime.proof["max_response_bytes"] = bound
    runtime.proof["bytes_per_day"] = 5 * bound
    runtime.delivery = SimpleNamespace(
        prepare=lambda *args: pytest.fail("oversized SDK table prepared")
    )
    runtime._deliver_plan(context.plan, context.release, runtime.feeds[0])
    assert len(calls) == expected_calls
    observed = len(
        canonical_bytes({"dates": ["2026-10-01"], "symbols": symbols, "values": [["10"] * 200]})
    )
    assert observed == 2449
    assert _quota(runtime, "2026-10-01") == (expected_calls, expected_bytes)
    assert raw.calls == [] and runtime.bundles == {}
    assert runtime.state.prepared("tw_equity_eod:" + symbols[0]) is None
    assert source.outcomes[-1]["reason"] == "source_error"
    runtime.http.close()


def test_finlab_valid_five_field_bundle_can_exceed_single_response_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, raw, _, context = _finlab_runtime(tmp_path, monkeypatch, "tw_equity_eod")
    runtime.proof["max_response_bytes"] = 100
    calls = _real_finlab_gateway(runtime, ("2330", "2317"))
    requests = runtime._acquire(
        context.release["members"][0],
        context.plan,
        context.plan["parts"][0],
        context.release,
        runtime.feeds[0],
    )
    assert len(calls) == 5 and len(requests) == 1
    assert 100 < len(raw.calls[0]) <= 5 * 100 + 64
    assert _quota(runtime, "2026-10-01") == (5, 500)
    runtime.http.close()


@pytest.mark.parametrize(
    "path", ["catalogue", "latest", "historical", "sdk_catalogue", "sdk_kbars", "sdk_usage"]
)
def test_other_provider_acquisition_paths_enforce_verified_byte_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    provider = (
        "twelve_data" if path == "catalogue" else "shioaji" if path.startswith("sdk") else "taifex"
    )
    runtime, _, raw = _runtime(tmp_path, provider=provider)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    oversized = b"x" * 2266
    runtime.http.close()
    runtime.http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=_Body(oversized)))
    )
    runtime._ensure_proof = lambda: None
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-secret")
    if path == "catalogue":
        acquire = runtime._catalogue
    elif path == "latest":
        runtime._reserve()

        def acquire() -> Any:
            return runtime._snapshot(TAIFEX_URL, provider_response=True)
    elif path == "historical":
        runtime._reserve()

        def acquire() -> Any:
            return fetch_report(
                runtime.http,
                target_date=date(2026, 10, 1),
                latest=False,
                max_response_bytes=1000,
                on_observed_bytes=runtime._observed_response,
            )
    elif path == "sdk_catalogue":
        runtime.sdk = SimpleNamespace(catalogue=lambda: [{"symbol": "x" * 2000}])
        acquire = runtime._catalogue
    else:
        kbars = {"ts": (), "Open": (), "High": (), "Low": (), "Close": (), "Volume": ()}
        if path == "sdk_kbars":
            kbars["padding"] = ("x" * 2000,)
        usage = 2266 if path == "sdk_usage" else 1
        runtime.sdk = SimpleNamespace(
            fetch_kbars=lambda *args: ShioajiKbarsSnapshot(kbars, 0, usage)
        )

        def acquire() -> Any:
            return runtime._acquire(
                _member(), _plan(), _plan()["parts"][0], {"release_id": "r"}, runtime.feeds[0]
            )

    with pytest.raises(ReadinessBlockedError, match="size bound"):
        acquire()
    assert _daily(runtime)[1] > 1000
    assert raw.calls == []
    runtime.http.close()


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
def test_restart_runtime_waits_entire_durable_account_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    first, _, _ = _runtime(tmp_path, provider=provider)
    clock = _Clock(monkeypatch)
    first.proof = _proof(clock.now())
    first.proof["requests_per_second"] = 1 / 120
    first._reserve()
    second, _, _ = _runtime(tmp_path, provider=provider)
    second.proof = first.proof.copy()
    second._reserve()
    assert clock.elapsed == 220 and clock.waits == [1] * 120
    assert _daily(second)[0] == 2
    first.http.close()
    second.http.close()


def test_shared_instances_take_one_transactional_permit(tmp_path: Path) -> None:
    states = [
        FullMarketState(tmp_path / "state.sqlite3", clock=lambda: 1790856000) for _ in range(2)
    ]
    barrier = Barrier(2)

    def take(state: FullMarketState) -> Any:
        barrier.wait()
        return state.reserve_acquisition(
            account="twelve_data",
            interval=120,
            requests=1,
            byte_count=1000,
            request_limit=10,
            byte_limit=10000,
            minute_limit=10,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(take, states))
    assert sorted(results) == [(0, 0, 1, "2026-10-01"), (120, 0, 0, "2026-10-01")]
    with states[0].connection() as db:
        assert db.execute("SELECT SUM(requests),SUM(bytes) FROM full_quota").fetchone()[:] == (
            2,
            1000,
        )
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] == 1790856120


def test_clock_rollback_preserves_pacing_and_expiry_stops_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, _ = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    runtime.proof["requests_per_second"] = 1 / 120
    runtime._reserve()
    clock.elapsed = 90
    runtime._reserve()
    assert clock.elapsed == 220 and clock.waits == [1] * 130
    runtime.proof["expires_at"] = (clock.now() + timedelta(seconds=2)).isoformat()
    with pytest.raises(ReadinessBlockedError, match="expired"):
        runtime._reserve()
    assert _daily(runtime)[0] == 2
    runtime.http.close()


def test_failed_minute_quota_preserves_daily_without_consuming_pacing_permit(
    tmp_path: Path,
) -> None:
    now = 1790856000.0
    state = FullMarketState(tmp_path / "state.sqlite3", clock=lambda: now)
    kwargs = dict(
        account="finlab",
        interval=1,
        requests=1,
        byte_count=1000,
        request_limit=10,
        byte_limit=10000,
        minute_limit=1,
    )
    state.reserve_acquisition(**kwargs)
    now += 1
    with pytest.raises(QuotaBlockedError):
        state.reserve_acquisition(**kwargs)
    with state.connection() as db:
        assert (
            db.execute("SELECT requests FROM full_quota WHERE length(window)=10").fetchone()[0] == 2
        )
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] == 1790856001


@pytest.mark.parametrize("guard", ["stop", "expiry"])
def test_control_or_expiry_during_transaction_keeps_reservation_without_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
) -> None:
    runtime, _, _ = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    original = runtime.state.reserve_acquisition

    def delayed(**kwargs: Any) -> Any:
        result = original(**kwargs)
        clock.elapsed += 2
        return result

    runtime.state.reserve_acquisition = delayed
    if guard == "stop":
        runtime.can_acquire = lambda: clock.elapsed < 101
    else:
        runtime.proof["expires_at"] = (clock.now() + timedelta(seconds=1)).isoformat()
    with pytest.raises(ReadinessBlockedError, match="stopped" if guard == "stop" else "expired"):
        runtime._reserve()
    assert _daily(runtime) == (1, 1000)
    runtime.http.close()


def test_sqlite_contention_preserves_actual_one_second_permit_spacing(tmp_path: Path) -> None:
    first, _, _ = _runtime(tmp_path)
    second, _, _ = _runtime(tmp_path)
    for runtime in (first, second):
        runtime.proof = _proof(datetime.now(timezone.utc))
        runtime.proof["requests_per_second"] = 1
    started = Barrier(3)

    def take(runtime: Any) -> float:
        started.wait()
        runtime._reserve()
        return time.monotonic()

    # Both processes enter with a pre-lock clock more than one permit old.
    with sqlite3.connect(first.state.path) as blocker, ThreadPoolExecutor(max_workers=2) as pool:
        blocker.execute("BEGIN IMMEDIATE")
        pending = [pool.submit(take, runtime) for runtime in (first, second)]
        started.wait()
        time.sleep(1.2)
        blocker.commit()
        actual_permits = sorted(future.result(timeout=5) for future in pending)
    assert actual_permits[1] - actual_permits[0] >= 0.98
    assert _daily(first) == (2, 2000)
    first.http.close()
    second.http.close()


@pytest.mark.parametrize("boundary", ["minute", "day"])
def test_sqlite_wait_uses_post_lock_quota_window_and_overage_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    runtime, _, _ = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    clock.elapsed = 59 if boundary == "minute" else 43199
    runtime.proof = _proof(clock.now())
    old_day = clock.now().date().isoformat()
    old_minute = clock.now().strftime("%Y-%m-%dT%H:%M")
    runtime.state.clock = lambda: clock.now().timestamp()
    entered = Event()
    original_connection = runtime.state.connection

    @contextmanager
    def traced_connection() -> Any:
        with original_connection() as db:
            db.set_trace_callback(
                lambda statement: entered.set() if statement == "BEGIN IMMEDIATE" else None
            )
            yield db

    monkeypatch.setattr(runtime.state, "connection", traced_connection)

    def take() -> None:
        runtime._reserve()

    # A real SQLite writer blocks reservation while UTC crosses a quota boundary.
    with sqlite3.connect(runtime.state.path) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        blocker.execute("BEGIN IMMEDIATE")
        pending = pool.submit(take)
        assert entered.wait(timeout=2)
        # The real SQLite trace confirms BEGIN has reached the write-lock boundary.
        clock.elapsed += 2
        expected_epoch = clock.now().timestamp()
        blocker.commit()
        pending.result(timeout=3)
    new_day = clock.now().date().isoformat()
    new_minute = clock.now().strftime("%Y-%m-%dT%H:%M")
    assert runtime.reservation_window == new_day
    with runtime.state.connection() as db:
        rows = {
            row["window"]: (row["requests"], row["bytes"])
            for row in db.execute("SELECT * FROM full_quota")
        }
        assert rows == {new_day: (1, 1000), new_minute: (1, 0)}
        assert old_minute not in rows
        if boundary == "day":
            assert old_day not in rows
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] > expected_epoch
    with pytest.raises(ReadinessBlockedError, match="size bound"):
        runtime._observed_response(1200)
    assert _daily(runtime) == (1, 1200)
    runtime.http.close()


@pytest.mark.parametrize("path", ["catalogue", "snapshot", "report"])
@pytest.mark.parametrize(
    "boundary", ["gzip", "encoding", "declared", "invalid", "negative", "wire", "identity"]
)
def test_http_catalogue_and_public_reports_enforce_exact_raw_body_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, boundary: str
) -> None:
    runtime, _, raw = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    runtime.proof["max_response_bytes"] = 20
    runtime._ensure_proof = lambda: None
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-secret")
    body = b'{"data":[]}'
    headers = {}
    if boundary == "gzip":
        body = gzip.compress(body)
        headers = {"Content-Encoding": "gzip", "Content-Length": str(len(body))}
        assert len(body) == 31
    elif boundary == "encoding":
        headers = {"Content-Encoding": "br"}
    elif boundary == "declared":
        headers = {"Content-Length": "21"}
    elif boundary in {"invalid", "negative"}:
        headers = {"Content-Length": "invalid" if boundary == "invalid" else "-1"}
    elif boundary == "wire":
        body += b" " * 21
    else:
        headers = {"Content-Length": str(len(body)), "Content-Encoding": "Identity"}
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers=headers, stream=_Body(body))

    runtime.http.close()
    runtime.http = httpx.Client(transport=httpx.MockTransport(handle))
    if path == "catalogue":
        acquire = runtime._catalogue
    else:
        runtime._reserve()
        if path == "snapshot":

            def acquire() -> Any:
                return runtime._snapshot(TAIFEX_URL, provider_response=True)
        else:

            def acquire() -> Any:
                return fetch_report(
                    runtime.http,
                    target_date=date(2026, 10, 1),
                    latest=False,
                    max_response_bytes=20,
                    on_observed_bytes=runtime._observed_response,
                    on_bounded_response=lambda size, declared: runtime._observed_response(
                        size, declared=declared
                    ),
                )

    if boundary == "identity":
        assert acquire() == ([] if path == "catalogue" else body)
        if path != "report":
            assert raw.calls == [body]
        if path == "catalogue":
            assert list(runtime.catalogue_evidence.values()) == [body]
    else:
        error = (
            ReadinessBlockedError
            if path != "report" or boundary in {"gzip", "declared", "wire"}
            else UniverseSnapshotError
        )
        message = (
            "Content-Encoding"
            if boundary == "encoding"
            else "Content-Length"
            if boundary in {"invalid", "negative"}
            else "size"
        )
        with pytest.raises(error, match=message):
            acquire()
        assert raw.calls == [] and runtime.catalogue_evidence == {} and runtime.bundles == {}
    assert len(calls) == 1
    assert _daily(runtime) == (1, len(body) if boundary == "wire" else 20)
    runtime.http.close()

    with runtime.state.connection() as db:
        violation = db.execute("SELECT * FROM full_capacity_violation").fetchone()
        if boundary in {"gzip", "declared", "wire"}:
            assert violation is not None
            assert violation["received_bytes"] == (len(body) if boundary == "wire" else 0)
            assert violation["declared_bytes"] == (
                31 if boundary == "gzip" else 21 if boundary == "declared" else 0
            )
        else:
            assert violation is None


@pytest.mark.parametrize("buffered", [True, False])
def test_official_universe_snapshot_enforces_enrolled_bound_and_latches_account(tmp_path, buffered):
    from findb_fetcher.account_governor import Permit
    from findb_fetcher.full_market_universe import NASDAQ_URLS

    runtime, _, _ = _runtime(tmp_path)
    state = FullMarketState(tmp_path / "shared-account.sqlite3")
    _, _, _, window = state.reserve_acquisition(
        account="shared",
        interval=0.001,
        requests=1,
        byte_count=100,
        request_limit=100,
        byte_limit=10000,
        minute_limit=100,
    )
    runtime.proof = {"max_response_bytes": 100}
    runtime.account_permit = Permit(state, "shared", window, 100, allocation_sha256="a" * 64)
    runtime._reserve = lambda: None

    def response(request):
        if buffered:
            return httpx.Response(200, content=b"x" * 250)
        return httpx.Response(200, stream=_Body(b"x" * 250))

    runtime.http.close()
    runtime.http = httpx.Client(transport=httpx.MockTransport(response))
    with pytest.raises(QuotaBlockedError, match="byte bound"):
        runtime._snapshot(NASDAQ_URLS[0])
    assert runtime.snapshot_objects == {}
    with state.connection() as db:
        assert (
            db.execute(
                "SELECT bytes FROM full_quota WHERE account='shared' AND window=?", (window,)
            ).fetchone()[0]
            == 250
        )
    with pytest.raises(QuotaBlockedError, match="capacity violation"):
        state.reserve_acquisition(
            account="shared",
            interval=0.001,
            requests=1,
            byte_count=100,
            request_limit=100,
            byte_limit=10000,
            minute_limit=100,
        )
    runtime.close()


@pytest.mark.parametrize("boundary", ["header", "transport"])
def test_official_snapshot_records_only_received_bytes_on_incomplete_body(tmp_path, boundary):
    from findb_fetcher.account_governor import Permit
    from findb_fetcher.full_market_universe import NASDAQ_URLS

    runtime, _, _ = _runtime(tmp_path)
    state = FullMarketState(tmp_path / "shared-account.sqlite3")
    _, _, _, window = state.reserve_acquisition(
        account="shared",
        interval=0.001,
        requests=1,
        byte_count=100,
        request_limit=100,
        byte_limit=10000,
        minute_limit=100,
    )
    runtime.proof = {"max_response_bytes": 100}
    runtime.account_permit = Permit(state, "shared", window, 100, allocation_sha256="a" * 64)
    runtime._reserve = lambda: None
    observed = []
    original_observer = runtime._observed_response

    def observe(size, **kwargs):
        observed.append(size)
        return original_observer(size, **kwargs)

    runtime._observed_response = observe

    class Partial(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * 50
            raise httpx.ReadError("offline interrupted body")

    headers = {"Content-Length": "250"} if boundary == "header" else {}
    stream = _Body(b"x" * 250) if boundary == "header" else Partial()
    runtime.http.close()
    runtime.http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, stream=stream))
    )
    with pytest.raises(QuotaBlockedError if boundary == "header" else httpx.ReadError):
        runtime._snapshot(NASDAQ_URLS[0])
    with state.connection() as db:
        # Keep reserved 100; do not invent the unread 250 or refund quota.
        assert (
            db.execute(
                "SELECT bytes FROM full_quota WHERE account='shared' AND window=?", (window,)
            ).fetchone()[0]
            == 100
        )
        row = db.execute("SELECT * FROM full_capacity_violation").fetchone()
        if boundary == "header":
            assert row["received_bytes"] == 0 and row["declared_bytes"] == 250
            assert row["observed_bytes"] == 250
        else:
            assert row is None
    assert runtime.snapshot_objects == {}
    assert observed == ([0] if boundary == "header" else [50])
    runtime.close()


def test_cached_taifex_report_still_requires_four_source_calls_per_member(tmp_path, monkeypatch):
    import ast
    from uuid import UUID

    from findb_fetcher import full_market_cli
    from findb_fetcher.providers.taifex import parse_report, report_members
    from test_full_market import _taifex

    runtime, _, _ = _runtime(tmp_path, provider="taifex")
    day = date(2026, 10, 1)
    raw = canonical_bytes(
        [_taifex(product=product, month="202610", session="一般") for product in ("TX", "MTX")]
    )
    rows = parse_report(raw, target_date=day)
    members = report_members(rows)
    for member, row in zip(members, rows, strict=True):
        member.update(
            session=row["session"], member_key=row["contract_code"] + ":" + row["session"]
        )
    runtime.futures[day.isoformat()] = (raw, rows, False)
    calls = []

    def poll(**kwargs):
        calls.append("control")
        return SimpleNamespace(desired_state="running")

    def authorize(*args):
        calls.append("authorization")
        return {"acquisition_allowed": True}

    # Execute the actual CLI callback, including its control/auth calls. The
    # installed readiness ACK is already fresh, so its throttled report is idle.
    callback = next(
        node
        for node in ast.walk(ast.parse(Path(full_market_cli.__file__).read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == "can_acquire"
    )
    namespace = {
        "os": full_market_cli.os,
        "report": lambda: None,
        "control": SimpleNamespace(poll=poll),
        "source": SimpleNamespace(request=authorize),
    }
    exec(
        compile(ast.Module(body=[callback], type_ignores=[]), "actual-cli-callback", "exec"),
        namespace,
    )
    monkeypatch.setenv("FULL_MARKET_ENABLED", "true")
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "full_market")

    def deliver(prepared):
        calls.append("ingest")
        return SimpleNamespace(run_id=UUID(int=1))

    def status(*args, **kwargs):
        calls.append("status")
        return SimpleNamespace(status="completed", failed_records=0)

    runtime.can_acquire = namespace["can_acquire"]
    runtime.delivery = SimpleNamespace(
        prepare=lambda request: request, deliver=deliver, get_run_status=status
    )
    plan = _plan()
    plan["dataset_key"] = "tw_futures_eod"
    plan["parts"][0]["member_keys"] = [member["member_key"] for member in members]
    runtime.state.record_plan(plan, "taifex")
    feed = {**runtime.feeds[0], "dataset_key": "tw_futures_eod", "slot_id": "taiwan_market_window"}
    try:
        assert runtime._deliver_plan(plan, {"release_id": "r", "members": members}, feed)
        assert calls == ["control", "authorization", "ingest", "status"] * len(members)
        assert len(members) == 2
        # A cached report needs no new provider permit/request for either member.
        with runtime.state.connection() as db:
            assert db.execute("SELECT count(*) FROM full_quota").fetchone()[0] == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["snapshot", "catalogue", "daily", "taifex-report"])
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    "buffered,boundary",
    [
        (False, "bounded"),
        (True, "bounded"),
        (False, "header"),
        (True, "header"),
        (False, "header-encoding"),
        (True, "header-encoding"),
        (False, "wire"),
        (True, "wire"),
        (False, "transport"),
        (False, "invalid-length"),
        (False, "encoding"),
    ],
)
def test_http_error_boundaries_use_actual_shared_account_before_body_failures(
    tmp_path,
    monkeypatch,
    path,
    status,
    buffered,
    boundary,
):
    from findb_fetcher import account_governor
    from findb_fetcher.account_governor import Permit
    from findb_fetcher.full_market_universe import NASDAQ_URLS

    runtime, _, raw_store = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    started = clock.now().timestamp()
    shared_path = tmp_path / "account" / "governor.sqlite3"
    shared = FullMarketState(shared_path)
    _, _, _, window = shared.reserve_acquisition(
        account="actual-provider-account",
        interval=0.1,
        requests=1,
        byte_count=10,
        request_limit=100,
        byte_limit=10000,
        minute_limit=100,
    )
    runtime.account_permit = Permit(
        shared, "actual-provider-account", window, 10, allocation_sha256="a" * 64
    )
    runtime.proof = {**_proof(clock.now()), "max_response_bytes": 10}
    runtime.reservation_window = window
    runtime._reserve = lambda: None
    runtime._ensure_proof = lambda: None
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "full_market")
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-fixture")
    headers = {"Content-Length": "100"} if boundary in {"header", "header-encoding"} else {}
    if boundary == "invalid-length":
        headers["Content-Length"] = "invalid"
    if boundary in {"encoding", "header-encoding"}:
        headers["Content-Encoding"] = "gzip"
    body = b"x" * (25 if boundary in {"header", "wire", "header-encoding"} else 5)
    observed = []
    original = runtime._observed_response

    def observe(size, **kwargs):
        observed.append((size, kwargs.get("declared", 0)))
        return original(size, **kwargs)

    runtime._observed_response = observe

    class Partial(httpx.SyncByteStream):
        def __iter__(self):
            yield b"12345"
            raise httpx.ReadError("offline interrupted HTTP error body")

    def response(request):
        if buffered:
            result = httpx.Response(status, content=body)
            result.headers.update(
                headers
            )  # Model already-decoded buffered content without inventing wire bytes.
            if boundary == "wire":
                del result.headers["Content-Length"]
            return result
        return httpx.Response(
            status, headers=headers, stream=Partial() if boundary == "transport" else _Body(body)
        )

    runtime.http.close()
    runtime.http = httpx.Client(transport=httpx.MockTransport(response))
    if path == "daily":
        runtime.td = TwelveDataClient(
            TwelveDataConfig(api_key="offline-fixture"), client=runtime.http
        )

    def acquire():
        if path == "snapshot":
            return runtime._snapshot(NASDAQ_URLS[0])
        if path == "catalogue":
            return runtime._catalogue()
        if path == "daily":
            return runtime._acquire(
                _member(), _plan(), _plan()["parts"][0], {"release_id": "r"}, runtime.feeds[0]
            )
        return fetch_report(
            runtime.http,
            target_date=date(2026, 10, 1),
            latest=False,
            max_response_bytes=10,
            on_observed_bytes=runtime._observed_response,
            on_bounded_response=lambda size, declared: runtime._observed_response(
                size, declared=declared
            ),
            on_http_rate_limited=runtime._rate_limited,
        )

    try:
        with pytest.raises(
            (
                ReadinessBlockedError,
                QuotaBlockedError,
                TwelveDataResponseError,
                UniverseSnapshotError,
                httpx.ReadError,
            )
        ):
            acquire()
        assert runtime.snapshot_objects == {} and raw_store.calls == []
        assert runtime.catalogue_evidence == {}
        expected_actual = 25 if boundary == "wire" or (boundary == "header" and buffered) else 0
        violated = boundary in {"header", "header-encoding", "wire"}
        with shared.connection() as db:
            row = db.execute(
                "SELECT * FROM full_quota WHERE account='actual-provider-account' AND window=?",
                (window,),
            ).fetchone()
            assert (row["requests"], row["bytes"]) == (1, max(10, expected_actual))
            pacing = db.execute(
                "SELECT next_at FROM full_pacing WHERE account='actual-provider-account'"
            ).fetchone()[0]
            assert pacing == started + (60 if status == 429 else 0.1)
            violation = db.execute("SELECT * FROM full_capacity_violation").fetchone()
            if violated:
                assert violation["account"] == "actual-provider-account"
                assert violation["received_bytes"] == expected_actual
                assert violation["declared_bytes"] == (
                    100 if boundary in {"header", "header-encoding"} else 0
                )
            else:
                assert violation is None
        with runtime.state.connection() as db:
            assert db.execute("SELECT count(*) FROM full_quota").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM full_pacing").fetchone()[0] == 0
        if path in {"snapshot", "taifex-report"} and boundary == "transport":
            assert observed == [(5, 0)]
        # A distinct consumer/process opens the shared governor after restart.
        restarted = FullMarketState(shared_path)

        def reserve():
            return restarted.reserve_acquisition(
                account="actual-provider-account",
                interval=0.1,
                requests=1,
                byte_count=10,
                request_limit=100,
                byte_limit=10000,
                minute_limit=100,
            )

        if violated:
            clock.elapsed += 61
            with pytest.raises(QuotaBlockedError, match="capacity violation"):
                reserve()
        else:
            wait, *_ = reserve()
            assert wait == pytest.approx(60 if status == 429 else 0.1, abs=1e-6)
            clock.elapsed += 60 if status == 429 else 1
            assert reserve()[0] == 0
        # Shared provider cooldown/violation does not stop prepared Source drain.
        allocation = tmp_path / "account.json"
        allocation.write_text('{"source_requests_per_minute":120}')
        monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(allocation))
        monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(shared_path))
        account_governor.source_permit("prepared-source-key")
    finally:
        runtime.close()


def test_shared_429_is_committed_before_secondary_observer_failure(tmp_path, monkeypatch):
    from findb_fetcher.account_governor import Permit
    from findb_fetcher.full_market_universe import NASDAQ_URLS

    runtime, _, _ = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    shared = FullMarketState(tmp_path / "account.sqlite3")
    runtime.account_permit = Permit(shared, "real-account", "2026-10-01", 10)
    runtime.proof = {"max_response_bytes": 10}
    runtime._reserve = lambda: None
    runtime._observed_response = lambda *args, **kwargs: (_ for _ in ()).throw(
        QuotaBlockedError("offline ledger failure")
    )
    runtime.http.close()
    runtime.http = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, stream=_Body(b"123")))
    )
    try:
        with pytest.raises(QuotaBlockedError, match="ledger failure"):
            runtime._snapshot(NASDAQ_URLS[0])
        with shared.connection() as db:
            assert (
                db.execute(
                    "SELECT next_at FROM full_pacing WHERE account='real-account'"
                ).fetchone()[0]
                == clock.now().timestamp() + 60
            )
    finally:
        runtime.close()


@pytest.mark.parametrize("consumer", ["pilot", "historical"])
@pytest.mark.parametrize(
    "status,boundary",
    [
        (200, "bounded"),
        (429, "bounded"),
        (503, "bounded"),
        (429, "header"),
        (503, "wire"),
        (429, "transport"),
    ],
)
def test_taifex_error_reader_uses_its_own_shared_permit_for_bounded_consumers(
    tmp_path, monkeypatch, consumer, status, boundary
):
    from findb_fetcher import account_governor
    from findb_fetcher.account_governor import Permit

    clock = _Clock(monkeypatch)
    shared = FullMarketState(tmp_path / "shared.sqlite3")
    _, _, _, window = shared.reserve_acquisition(
        account="taifex-shared",
        interval=0.1,
        requests=1,
        byte_count=10,
        request_limit=100,
        byte_limit=10000,
        minute_limit=100,
        allocation="other",
        allocation_request_limit=100,
        allocation_byte_limit=10000,
    )
    permit = Permit(
        shared, "taifex-shared", window, 10, allocation="other", allocation_sha256="a" * 64
    )
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", consumer)
    monkeypatch.setattr(
        account_governor,
        "provider_permit",
        lambda provider: permit if provider == "taifex" else pytest.fail("wrong account provider"),
    )

    class Partial(httpx.SyncByteStream):
        def __iter__(self):
            yield b"12345"
            raise httpx.ReadError("offline partial report")

    headers = {"Content-Length": "100"} if boundary == "header" else {}
    raw = b"x" * (25 if boundary == "wire" else 5)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status, headers=headers, stream=Partial() if boundary == "transport" else _Body(raw)
            )
        )
    ) as http:
        if status == 200:
            assert (
                fetch_report(
                    http, target_date=date(2026, 10, 1), latest=False, max_response_bytes=10
                )
                == raw
            )
        else:
            with pytest.raises((UniverseSnapshotError, QuotaBlockedError, httpx.ReadError)):
                fetch_report(
                    http, target_date=date(2026, 10, 1), latest=False, max_response_bytes=10
                )
    with shared.connection() as db:
        quota = db.execute(
            "SELECT requests,bytes FROM full_quota WHERE account='taifex-shared' AND window=?",
            (window,),
        ).fetchone()
        assert tuple(quota) == (1, 25 if boundary == "wire" else 10)
        if status == 429:
            assert (
                db.execute(
                    "SELECT next_at FROM full_pacing WHERE account='taifex-shared'"
                ).fetchone()[0]
                == clock.now().timestamp() + 60
            )
        if boundary == "header":
            violation = db.execute("SELECT * FROM full_capacity_violation").fetchone()
            assert violation["received_bytes"] == 0 and violation["declared_bytes"] == 100


@pytest.mark.parametrize("length", ["100", "invalid", "-1", None])
def test_buffered_encoded_error_uses_measured_wire_bytes_instead_of_decoded_length(length):
    from findb_fetcher.http_response import BoundedResponseError, read_identity_response

    wire = gzip.compress(b"x" * 250)
    headers = {"Content-Encoding": "gzip"}
    if length is not None:
        headers["Content-Length"] = length
    response = httpx.Response(429, headers=headers, stream=_Body(wire))
    response.read()  # Model an already-buffered client with a real raw-byte counter.
    assert len(response.content) == 250 and response.num_bytes_downloaded == len(wire)
    with pytest.raises(BoundedResponseError) as caught:
        read_identity_response(response, bound=10)
    assert caught.value.observed_bytes == len(wire)
    assert caught.value.observed_bytes != 250
    assert caught.value.declared_bytes == (100 if length == "100" else 0)


@pytest.mark.parametrize("consumer", ["pilot", "historical"])
@pytest.mark.parametrize(
    "buffered,boundary",
    [
        (buffered, boundary)
        for buffered in [False, True]
        for boundary in [
            "bounded",
            "header",
            "wire",
            "transport",
            "encoding",
            "observer",
            "callback",
        ]
        if not (buffered and boundary == "transport")
    ],
)
def test_twelve_data_own_permit_persists_429_before_every_error_boundary(
    tmp_path, monkeypatch, consumer, buffered, boundary
):
    from findb_fetcher import account_governor
    from findb_fetcher.account_governor import Permit

    clock = _Clock(monkeypatch)
    started = clock.now().timestamp()
    path = tmp_path / "shared.sqlite3"
    shared = FullMarketState(path)
    _, _, _, window = shared.reserve_acquisition(
        account="actual-twelve-data-account",
        interval=0.1,
        requests=1,
        byte_count=10,
        request_limit=100,
        byte_limit=10000,
        minute_limit=100,
    )
    permit = Permit(shared, "actual-twelve-data-account", window, 10, allocation_sha256="a" * 64)
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", consumer)
    monkeypatch.setattr(account_governor, "provider_permit", lambda provider: permit)
    observed = []
    original = permit.observe

    def observe(size, **kwargs):
        observed.append((size, kwargs.get("declared", 0)))
        if boundary == "observer":
            raise RuntimeError("offline observer failure")
        original(size, **kwargs)

    permit.observe = observe

    class Partial(httpx.SyncByteStream):
        def __iter__(self):
            yield b"12345"
            raise httpx.ReadError("offline interrupted Twelve Data error body")

    def response(_):
        headers = {"Content-Length": "100"} if boundary == "header" else {}
        body = b"x" * (25 if boundary == "wire" else 5)
        if buffered:
            result = httpx.Response(429, content=body)
            result.headers.update(headers)
        else:
            result = httpx.Response(
                429, headers=headers, stream=Partial() if boundary == "transport" else _Body(body)
            )
        if boundary == "encoding":
            result.headers["Content-Encoding"] = "gzip"
        return result

    def callback():
        raise RuntimeError("offline callback failure")

    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        client = TwelveDataClient(TwelveDataConfig(api_key="offline-secret"), client=http)
        with pytest.raises((TwelveDataResponseError, QuotaBlockedError, RuntimeError)) as caught:
            client.fetch_daily(
                "AAPL", on_http_rate_limited=callback if boundary == "callback" else None
            )
    if boundary == "transport":
        assert caught.value.observed_bytes == 5
        assert observed == [(5, 0)]
    actual = 25 if boundary == "wire" else 5 if boundary == "header" and buffered else 0
    with shared.connection() as db:
        assert tuple(db.execute("SELECT requests,bytes FROM full_quota").fetchone()) == (
            1,
            max(10, actual),
        )
        assert (
            db.execute(
                "SELECT next_at FROM full_pacing WHERE account='actual-twelve-data-account'"
            ).fetchone()[0]
            == started + 60
        )
        violation = db.execute("SELECT * FROM full_capacity_violation").fetchone()
        if boundary in {"header", "wire"}:
            assert violation["received_bytes"] == actual
            assert violation["declared_bytes"] == (
                100 if boundary == "header" else 25 if buffered else 0
            )
        else:
            assert violation is None
    restarted = FullMarketState(path)

    def reserve():
        return restarted.reserve_acquisition(
            account="actual-twelve-data-account",
            interval=0.1,
            requests=1,
            byte_count=10,
            request_limit=100,
            byte_limit=10000,
            minute_limit=100,
        )

    if violation:
        clock.elapsed += 61
        with pytest.raises(QuotaBlockedError, match="capacity violation"):
            reserve()
    else:
        assert reserve()[0] == pytest.approx(60)
        clock.elapsed += 60
        assert reserve()[0] == 0
    allocation = tmp_path / "account.json"
    allocation.write_text('{"source_requests_per_minute":120}')
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(allocation))
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    account_governor.source_permit("prepared-source-key")


@pytest.mark.parametrize("status", [201, 206])
def test_twelve_data_success_shaped_non_200_is_accounted_then_rejected(
    tmp_path, monkeypatch, status
):
    from findb_fetcher import account_governor
    from findb_fetcher.account_governor import Permit

    shared = FullMarketState(tmp_path / "shared.sqlite3")
    permit = Permit(shared, "real-account", "2026-10-01", 100)
    observed = []
    monkeypatch.setattr(permit, "observe", lambda size, **kwargs: observed.append(size))
    monkeypatch.setattr(account_governor, "provider_permit", lambda _: permit)
    raw = b'{"status":"ok","values":[]}'
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, content=raw))
    ) as http:
        client = TwelveDataClient(TwelveDataConfig(api_key="offline-secret"), client=http)
        with pytest.raises(TwelveDataResponseError) as caught:
            client.fetch_daily("AAPL")
    assert observed == [len(raw)]
    assert caught.value.status_code == status
    assert caught.value.observed_bytes == len(raw)


@pytest.mark.parametrize("status", [201, 206])
def test_full_twelve_data_non_200_never_persists_provider_payload(tmp_path, monkeypatch, status):
    runtime, _, raw_store = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    runtime.proof = _proof(clock.now())
    runtime._ensure_proof = lambda: None
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-secret")
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"status": "ok", "values": []})
        )
    ) as http:
        runtime.td = TwelveDataClient(TwelveDataConfig(api_key="offline-secret"), client=http)
        try:
            with pytest.raises(TwelveDataResponseError) as caught:
                runtime._acquire(
                    _member(), _plan(), _plan()["parts"][0], {"release_id": "r"}, runtime.feeds[0]
                )
            assert caught.value.status_code == status
            assert caught.value.observed_bytes > 0
            assert _daily(runtime) == (1, 1000)
            assert raw_store.calls == []
            assert runtime.state.prepared("fp1:123:AAPL") is None
        finally:
            runtime.close()


@pytest.mark.parametrize("status", [200, 503])
def test_twelve_data_partial_transport_preserves_known_received_bytes(status):
    class Partial(httpx.SyncByteStream):
        def __iter__(self):
            yield b"12345"
            raise httpx.ReadError("offline interrupted body")

    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, stream=Partial()))
    ) as http:
        client = TwelveDataClient(TwelveDataConfig(api_key="offline-secret"), client=http)
        with pytest.raises(TwelveDataResponseError) as caught:
            client.fetch_daily("AAPL")
    assert caught.value.status_code == status
    assert caught.value.observed_bytes == 5
    assert caught.value.declared_bytes == 0
    assert caught.value.__cause__.observed_bytes == 5
