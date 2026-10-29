"""Whole-day historical recovery, evidence and bounded retry regressions."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_universe import TAIFEX_HISTORY_URL, canonical_bytes, checksum
from findb_fetcher.historical_backfill import HistoricalWorkDeferredError, HistoricalWorkItem
from findb_fetcher.raw_storage import RawObject, RawStorageUploadError
from findb_fetcher.taifex_historical import TaifexHistoricalRunner

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 1, 10, tzinfo=timezone.utc).timestamp()


def item():
    return HistoricalWorkItem(
        UUID(int=1),
        UUID(int=2),
        "request-key",
        "taifex",
        "tw_futures_eod",
        "TW",
        date(2026, 10, 1),
        UUID(int=3),
        datetime.fromtimestamp(NOW + 900, timezone.utc),
    )


def report(product, *, no_trade=False, month="202610F1"):
    values = (
        ["-", "-", "-", "-", "0", "-", "-"]
        if no_trade
        else ["100", "110", "90", "105", "37", "-", "-"]
    )
    header = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,交易時段\n"
    return (header + ",".join(["2026/10/01", product, month, *values, "一般"]) + "\n").encode(
        "cp950"
    )


class RawStore:
    def __init__(self):
        self.raw = []
        self.failures = 0

    def persist(self, raw, **kwargs):
        self.raw.append((raw, kwargs))
        if self.failures:
            self.failures -= 1
            raise RawStorageUploadError("temporary", retryable=True)
        sha = checksum(raw)
        extension = "csv" if kwargs.get("payload_format") == "csv_cp950" else "json"
        return RawObject(
            f"r2://{'a' * 32}/raw/taifex/tw_futures_eod/123/{sha}.{extension}", sha, len(raw)
        )


class Source:
    def __init__(self):
        self.requests = []
        self.fail_delivery = False
        self.status = "completed"

    def prepare(self, body):
        registry().validate("futures_eod", 1, body)
        return SimpleNamespace(body=canonical_bytes(body))

    def deliver(self, prepared):
        import json

        self.requests.append(prepared.body)
        if self.fail_delivery:
            self.fail_delivery = False
            raise KeyboardInterrupt()
        self.rows = len(json.loads(prepared.body)["payload"]["data"])
        return SimpleNamespace(run_id=UUID(int=10))

    def get_run_status(self, run, **kwargs):
        return SimpleNamespace(
            status=self.status, total_records=self.rows, success_records=self.rows, failed_records=0
        )


def registry():
    return ContractRegistry(ROOT / "contracts")


def runner(tmp_path, handler, *, source=None, raw=None, clock=None):
    current = clock or [NOW]
    return TaifexHistoricalRunner(
        path=tmp_path / "state.sqlite3",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        source=source or Source(),
        raw_store=raw or RawStore(),
        registry=registry(),
        feed={"slot_id": "taiwan_market_window"},
        identity="local:source",
        clock=lambda: current[0],
        sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
    )


def test_all_products_raw_first_and_source_batch_recovers_without_acquisition(tmp_path):
    calls = []

    def handler(request):
        from urllib.parse import parse_qs

        calls.append(request)
        product = parse_qs(request.content.decode())["commodity_id"][0]
        assert str(request.url) == TAIFEX_HISTORY_URL and request.method == "POST"
        return httpx.Response(200, content=report(product, no_trade=product == "TF"))

    source, raw = Source(), RawStore()
    source.fail_delivery = True
    first = runner(tmp_path, handler, source=source, raw=raw)
    with pytest.raises(KeyboardInterrupt):
        first.run(item())
    assert len(calls) == 5 and len(raw.raw) == 6
    assert [kwargs.get("payload_format", "json") for _, kwargs in raw.raw] == [
        "csv_cp950",
        "csv_cp950",
        "csv_cp950",
        "csv_cp950",
        "csv_cp950",
        "json",
    ]
    second = runner(
        tmp_path, lambda _: pytest.fail("recovery fetched provider"), source=source, raw=raw
    )
    result = second.run(replace(item(), lease_token=UUID(int=4)))
    assert result.run_id == UUID(int=10)
    assert len(result.result_evidence["rows"]) == 5
    assert all(report["ref"].endswith(".csv") for report in result.result_evidence["reports"])
    assert result.result_evidence["manifest"]["ref"].endswith(".json")
    assert source.requests[0] == source.requests[1]
    assert len(raw.raw) == 6


def test_explicit_no_data_day_has_durable_manifest_without_source_run(tmp_path):
    def handler(request):
        from urllib.parse import parse_qs

        return httpx.Response(
            200,
            content=report(parse_qs(request.content.decode())["commodity_id"][0], no_trade=True),
        )

    source = Source()
    result = runner(tmp_path, handler, source=source).run(item())
    assert result.run_id is None and not source.requests
    assert len(result.result_evidence["reports"]) == 5


def test_transient_provider_and_raw_retry_without_repeating_successful_fetch(tmp_path):
    calls = []

    def handler(request):
        from urllib.parse import parse_qs

        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200, content=report(parse_qs(request.content.decode())["commodity_id"][0])
        )

    raw = RawStore()
    raw.failures = 1
    runtime = runner(tmp_path, handler, raw=raw)
    assert runtime.run(item()).run_id is not None
    assert len(calls) == 6
    assert raw.raw[0][0] == raw.raw[1][0]


def test_retry_state_survives_lease_boundary_and_is_bounded(tmp_path):
    clock = [NOW]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    runtime = runner(tmp_path, handler, clock=clock)
    short = replace(item(), lease_expires_at=datetime.fromtimestamp(NOW + 50, timezone.utc))
    with pytest.raises(HistoricalWorkDeferredError):
        runtime.run(short)
    assert len(calls) == 1
    clock[0] += 60
    with pytest.raises(ValueError, match="exhausted"):
        runner(tmp_path, handler, clock=clock).run(item())
    assert len(calls) == 3


@pytest.mark.parametrize(
    "raw",
    [
        b"<html>error</html>",
        report("TX", month="202610/202611"),
        report("TX").replace(b"2026/10/01", b"2026/09/30"),
    ],
)
def test_invalid_reports_stop_before_delivery_but_preserve_raw(tmp_path, raw):
    store, source = RawStore(), Source()
    with pytest.raises(ValueError):
        runner(tmp_path, lambda _: httpx.Response(200, content=raw), source=source, raw=store).run(
            item()
        )
    assert store.raw[0][0] == raw and not source.requests


def test_nonretryable_http_and_checkpoint_identity(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403)

    with pytest.raises(RuntimeError):
        runner(tmp_path, handler).run(item())
    assert len(calls) == 1
    with pytest.raises(ValueError, match="identity differs"):
        TaifexHistoricalRunner(
            path=tmp_path / "state.sqlite3",
            http=None,
            source=None,
            raw_store=None,
            registry=registry(),
            feed={},
            identity="production:other-source",
        )


def test_long_cooldown_defers_without_network_and_preserves_budget(tmp_path, monkeypatch):
    runtime = runner(tmp_path, lambda _: pytest.fail("cooldown invoked provider"))
    monkeypatch.setattr(runtime, "_cooldown", lambda: NOW + 3600)
    with pytest.raises(HistoricalWorkDeferredError):
        runtime.run(item())


def test_source_completion_has_durable_bounded_retries(tmp_path):
    from findb_fetcher.historical_backfill import HistoricalControlError

    def handler(request):
        from urllib.parse import parse_qs

        return httpx.Response(
            200,
            content=report(parse_qs(request.content.decode())["commodity_id"][0], no_trade=True),
        )

    runtime = runner(tmp_path, handler)
    result = runtime.run(item())

    class Control:
        calls = 0

        def complete(self, _item, _result):
            self.calls += 1
            if self.calls < 3:
                raise HistoricalControlError("temporary", status_code=503)

    control = Control()
    runtime.complete(control, item(), result)
    assert control.calls == 3


@pytest.mark.parametrize(
    "operation", ["fetch:TX", "raw:TX", "manifest", "delivery", "terminal", "completion"]
)
def test_hard_crashes_consume_durable_budget_before_dispatch(tmp_path, operation):
    import json
    import sqlite3

    calls = []
    clock = [NOW]

    def interrupted():
        calls.append(clock[0])
        raise KeyboardInterrupt()

    for attempt in range(3):
        runtime = runner(tmp_path, lambda _: pytest.fail("unexpected HTTP"), clock=clock)
        with sqlite3.connect(runtime.path) as db:
            saved = db.execute(
                "SELECT body FROM work WHERE item_id=?", (str(item().item_id),)
            ).fetchone()
        state = json.loads(saved[0]) if saved else {}
        with pytest.raises(KeyboardInterrupt):
            runtime._operation(item(), state, operation, interrupted)
    runtime = runner(tmp_path, lambda _: pytest.fail("unexpected HTTP"), clock=clock)
    with sqlite3.connect(runtime.path) as db:
        state = json.loads(
            db.execute("SELECT body FROM work WHERE item_id=?", (str(item().item_id),)).fetchone()[
                0
            ]
        )
    assert state["operations"][operation]["attempts"] == 3
    assert state["operations"][operation]["next_at"] == NOW + 270
    with pytest.raises(ValueError, match="exhausted"):
        runtime._operation(item(), state, operation, interrupted)
    assert calls == [NOW, NOW + 30, NOW + 150]


@pytest.mark.parametrize(
    "operation", ["fetch:TX", "raw:TX", "manifest", "delivery", "terminal", "completion"]
)
def test_interrupted_second_dispatch_preserves_120_second_floor_across_reclaim(tmp_path, operation):
    import json
    import sqlite3

    clock = [NOW + 863]
    runtime = runner(tmp_path, lambda _: pytest.fail("unexpected HTTP"), clock=clock)
    state = {"operations": {operation: {"attempts": 1, "next_at": NOW + 30}}}

    def interrupted():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        runtime._operation(item(), state, operation, interrupted)
    with sqlite3.connect(runtime.path) as db:
        saved = json.loads(db.execute("SELECT body FROM work").fetchone()[0])
    assert saved["operations"][operation] == {"attempts": 2, "next_at": NOW + 983}
    runtime.http.close()
    clock[0] = NOW + 901
    runtime = runner(tmp_path, lambda _: pytest.fail("unexpected HTTP"), clock=clock)
    renewed = replace(
        item(),
        lease_token=UUID(int=44),
        lease_expires_at=datetime.fromtimestamp(NOW + 1800, timezone.utc),
    )
    calls = []
    runtime._operation(renewed, saved, operation, lambda: calls.append(clock[0]))
    assert calls == [NOW + 983]


@pytest.mark.parametrize(("fault", "delay"), [(429, 60), (503, 30), ("transport", 30)])
def test_live_authorization_transients_use_durable_single_dispatch_retries(tmp_path, fault, delay):
    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.historical_backfill import HistoricalControlClient

    clock = [NOW]
    calls = []

    def respond(request):
        calls.append(clock[0])
        if len(calls) == 1:
            if fault == "transport":
                raise httpx.ConnectError("temporary", request=request)
            return httpx.Response(fault, headers={"Retry-After": "60"} if fault == 429 else {})
        checked_at = datetime.now(timezone.utc).isoformat()
        body = {
            "acquisition_allowed": True,
            "provider": "taifex",
            "checked_at": checked_at,
            "max_age_seconds": 5,
            "item_id": str(item().item_id),
            "request_id": str(item().request_id),
            "dataset_key": item().dataset_key,
            "lease_expires_at": datetime.fromtimestamp(clock[0] + 900, timezone.utc).isoformat(),
        }
        return httpx.Response(
            200, json=body, headers={"X-FinDB-Acquisition-Checked-At": checked_at}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        control = HistoricalControlClient(
            FetcherConfig("https://source.example", "test-key", max_attempts=9),
            provider="taifex",
            client=http,
        )
        runtime = runner(
            tmp_path, lambda _: pytest.fail("unexpected provider request"), clock=clock
        )
        result = runtime._operation(item(), {}, "fetch:TX", lambda: control.authorize(item()))
    assert result.provider == "taifex"
    assert calls == [NOW, NOW + delay]


def test_authorization_retry_after_survives_lease_reclaim(tmp_path):
    import json
    import sqlite3

    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.historical_backfill import HistoricalControlClient

    clock = [NOW]
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "1000"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        control = HistoricalControlClient(
            FetcherConfig("https://source.example", "test-key", max_attempts=9),
            provider="taifex",
            client=http,
        )
        runtime = runner(
            tmp_path, lambda _: pytest.fail("unexpected provider request"), clock=clock
        )
        with pytest.raises(HistoricalWorkDeferredError):
            runtime._operation(item(), {}, "fetch:TX", lambda: control.authorize(item()))
        with sqlite3.connect(runtime.path) as db:
            state = json.loads(db.execute("SELECT body FROM work").fetchone()[0])
        assert state["operations"]["fetch:TX"] == {"attempts": 1, "next_at": NOW + 1000}
        clock[0] = NOW + 900
        renewed = replace(
            item(),
            lease_token=UUID(int=5),
            lease_expires_at=datetime.fromtimestamp(NOW + 2000, timezone.utc),
        )
        with pytest.raises(HistoricalWorkDeferredError):
            runtime._operation(renewed, state, "fetch:TX", lambda: control.authorize(renewed))
        assert len(calls) == 2 and clock[0] == NOW + 1000


@pytest.mark.parametrize("header_kind", ["seconds", "date"])
def test_completion_retry_after_is_persisted_and_defers_at_lease_boundary(
    tmp_path, monkeypatch, header_kind
):
    import json
    import sqlite3
    from email.utils import format_datetime

    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.historical_backfill import HistoricalControlClient

    clock = [NOW]
    monkeypatch.setattr("findb_fetcher.full_market_client.time.time", lambda: clock[0])

    def provider(request):
        from urllib.parse import parse_qs

        return httpx.Response(
            200,
            content=report(parse_qs(request.content.decode())["commodity_id"][0], no_trade=True),
        )

    runtime = runner(tmp_path, provider, clock=clock)
    result = runtime.run(item())
    calls = []
    delay = (
        "1000"
        if header_kind == "seconds"
        else format_datetime(datetime.fromtimestamp(NOW + 1000, timezone.utc), usegmt=True)
    )

    def respond(request):
        calls.append(clock[0])
        return httpx.Response(429, headers={"Retry-After": delay})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        control = HistoricalControlClient(
            FetcherConfig("https://source.example", "test-key"), client=http
        )
        with pytest.raises(HistoricalWorkDeferredError):
            runtime.complete(control, item(), result)
    with sqlite3.connect(runtime.path) as db:
        state = json.loads(db.execute("SELECT body FROM work").fetchone()[0])
    assert calls == [NOW]
    assert state["operations"]["completion"] == {"attempts": 1, "next_at": NOW + 1000}


def test_run_once_slow_authorization_body_has_one_dispatch_per_durable_attempt(
    tmp_path, monkeypatch
):
    import json
    import time

    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.historical_backfill import HistoricalControlClient
    from findb_fetcher.historical_backfill import _run_once as run_once

    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account.sqlite3"))
    calls = []
    failed = []

    class SlowBody(httpx.SyncByteStream):
        def __init__(self, body):
            self.body = body

        def __iter__(self):
            time.sleep(0.065)
            yield canonical_bytes(self.body)

    def respond(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "item_id": str(item().item_id),
                    "request_id": str(item().request_id),
                    "request_key": item().request_key,
                    "provider": "taifex",
                    "dataset_key": item().dataset_key,
                    "market": "TW",
                    "trade_date": item().trade_date.isoformat(),
                    "lease_token": str(item().lease_token),
                    "lease_expires_at": item().lease_expires_at.isoformat(),
                },
            )
        if request.url.path.endswith("/complete"):
            failed.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True})
        calls.append(request)
        checked_at = datetime.now(timezone.utc)
        body = {
            "acquisition_allowed": True,
            "provider": "taifex",
            "checked_at": checked_at.isoformat(),
            "max_age_seconds": 5,
            "item_id": str(item().item_id),
            "request_id": str(item().request_id),
            "dataset_key": item().dataset_key,
            "lease_expires_at": (checked_at + timedelta(minutes=15)).isoformat(),
        }
        return httpx.Response(
            200,
            stream=SlowBody(body),
            headers={"X-FinDB-Acquisition-Checked-At": checked_at.isoformat()},
        )

    runtime = runner(tmp_path, lambda _: pytest.fail("expired dispatch grant reached provider"))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        control = HistoricalControlClient(
            FetcherConfig("https://source.example", "test-key", max_attempts=9),
            provider="taifex",
            client=http,
        )
        assert run_once(control, runtime)
    assert len(calls) == 3
    assert failed[0]["status"] == "failed"


def test_generic_authorization_window_retries_remain_three(tmp_path):
    import time

    from findb_fetcher.acquisition import Acquisition, Authorization, authorization_scope
    from findb_fetcher.full_market_state import QuotaBlockedError

    calls = []

    def authorize():
        calls.append(True)
        return Authorization(time.monotonic() - 0.1, time.time(), time.time(), provider="taifex")

    with authorization_scope(authorize, state_path=tmp_path / "generic-account.sqlite3"):
        with Acquisition("taifex") as operation:
            with pytest.raises(QuotaBlockedError):
                operation.start()
    assert len(calls) == 3


@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.parametrize("header_kind", ["seconds", "date"])
@pytest.mark.parametrize("delay", [600, 1000])
def test_provider_retry_after_is_durable_and_survives_reclaim(
    tmp_path, monkeypatch, status, header_kind, delay
):
    import json
    import sqlite3
    from email.utils import format_datetime
    from urllib.parse import parse_qs

    clock = [NOW]
    monkeypatch.setattr("findb_fetcher.providers.taifex.time.time", lambda: clock[0])
    calls = []
    header = (
        str(delay)
        if header_kind == "seconds"
        else format_datetime(datetime.fromtimestamp(NOW + delay, timezone.utc), usegmt=True)
    )

    def respond(request):
        calls.append(clock[0])
        if len(calls) == 1:
            return httpx.Response(status, headers={"Retry-After": header})
        return httpx.Response(
            200, content=report(parse_qs(request.content.decode())["commodity_id"][0])
        )

    runtime = runner(tmp_path, respond, clock=clock)
    work = item()
    if delay > 900:
        with pytest.raises(HistoricalWorkDeferredError):
            runtime.run(work)
        with sqlite3.connect(runtime.path) as db:
            state = json.loads(db.execute("SELECT body FROM work").fetchone()[0])
        assert calls == [NOW]
        assert state["operations"]["fetch:TX"] == {"attempts": 1, "next_at": NOW + delay}
        runtime.http.close()
        runtime = runner(tmp_path, respond, clock=clock)
        work = replace(
            work,
            lease_token=UUID(int=40),
            lease_expires_at=datetime.fromtimestamp(NOW + delay + 900, timezone.utc),
        )
    result = runtime.run(work)
    assert result.run_id == UUID(int=10)
    assert calls == [NOW] + [NOW + delay] * 5


@pytest.mark.parametrize("value", [None, "nonsense", "-1", "NaN", "inf", "0"])
def test_provider_invalid_retry_after_uses_durable_default(tmp_path, value):
    from urllib.parse import parse_qs

    clock = [NOW]
    calls = []

    def respond(request):
        calls.append(clock[0])
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": value} if value else {})
        return httpx.Response(
            200, content=report(parse_qs(request.content.decode())["commodity_id"][0])
        )

    assert runner(tmp_path, respond, clock=clock).run(item()).run_id == UUID(int=10)
    assert calls == [NOW] + [NOW + 30] * 5
