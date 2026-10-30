"""Backend outages wait while active ownership and original budgets stay fenced."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import MethodType, SimpleNamespace

import httpx
import pytest

from findb_fetcher import acquisition as acq
from findb_fetcher import full_market_cli as daily
from findb_fetcher import historical_backfill_cli as historical
from findb_fetcher import provider_work as queue
from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.full_market_runtime import _governed
from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.historical_backfill import HistoricalControlError
from findb_fetcher.scheduler_control import (
    SchedulerControlClient,
    SchedulerControlProtocolError,
    SchedulerControlResponseError,
    SchedulerControlTransportError,
    scheduler_stop_event,
)
from findb_fetcher.source_recovery import (
    SourceWaitStoppedError,
    retry_source,
    source_retry_sleep,
    transient_source_error,
)
from provider_work_support import owned_unit_work
from test_full_market import _member, _plan, _runtime


class Stop:
    def __init__(self, *, after=None):
        self.done = False
        self.waits = []
        self.after = after

    def is_set(self):
        return self.done

    def set(self):
        self.done = True

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.after is not None and len(self.waits) >= self.after:
            self.set()
        return self.done


@pytest.mark.parametrize("kind", ["governance", "control"])
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, 521, 522, 524, 599])
def test_actual_source_clients_retry_transient_statuses(monkeypatch, kind, status):
    calls, waits = [], []
    monkeypatch.setattr(time, "sleep", waits.append)

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={}) if len(calls) < 3 else httpx.Response(200, json={})

    config = FetcherConfig("https://source.example", "private", max_attempts=3)
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        if kind == "governance":
            result = FullMarketSourceClient(config, client=http).request("GET", "authorization")
        else:
            result = SchedulerControlClient(
                config, "full_market_finlab_v1", client=http, sleep=waits.append
            )._request(b"{}")
    assert result == {}
    assert len(calls) == 3
    assert waits == [1, 2]


@pytest.mark.parametrize("kind", ["governance", "control"])
def test_actual_source_clients_bound_exhaustion_and_reject_permanent_status(monkeypatch, kind):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    for status, count in [(521, 3), (401, 1), (403, 1)]:
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(status, json={})

        config = FetcherConfig("https://source.example", "private", max_attempts=3)
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            with pytest.raises((FullMarketProtocolError, SchedulerControlResponseError)) as error:
                if kind == "governance":
                    FullMarketSourceClient(config, client=http).request("GET", "authorization")
                else:
                    SchedulerControlClient(
                        config, "full_market_finlab_v1", client=http, sleep=lambda _: None
                    )._request(b"{}")
        assert error.value.status_code == status
        assert len(calls) == count


@pytest.mark.parametrize(
    "error",
    [
        FullMarketProtocolError("malformed"),
        FullMarketProtocolError("auth", status_code=403),
        SchedulerControlProtocolError("malformed"),
        SchedulerControlResponseError(401),
        ValueError("secret"),
    ],
)
def test_permanent_errors_are_not_retried(error):
    calls = []

    def operation():
        calls.append(1)
        raise error

    with pytest.raises(type(error)):
        retry_source(operation, stopped=Stop(), run_forever=True)
    assert len(calls) == 1
    assert not transient_source_error(error)


def test_outer_backoff_is_bounded_stoppable_and_sanitized(capsys):
    stopped = Stop(after=7)

    def unavailable():
        raise httpx.ConnectError("https://private:secret@source.invalid")

    with pytest.raises(SourceWaitStoppedError):
        retry_source(unavailable, stopped=stopped, run_forever=True, max_delay=5)
    assert stopped.waits == [1, 2, 4, 5, 5, 5, 5]
    output = capsys.readouterr().err
    assert output.count("fetcher_source_wait reason=source_unavailable") == 7
    assert "secret" not in output and "source.invalid" not in output


@pytest.mark.parametrize(
    "stage", ["drain", "idle", "gate", "start", "cycle", "completion", "error_report"]
)
def test_current_daily_worker_recovers_each_idle_source_boundary(stage):
    stopped, events = Stop(), []
    failed = False
    cycle_count = 0

    def tick(name):
        nonlocal failed
        events.append(name)
        if name == stage and not failed:
            failed = True
            raise FullMarketProtocolError("private", status_code=521)

    def poll(**kwargs):
        name = (
            "start"
            if "cycle_started_at" in kwargs
            else (
                "completion"
                if "cycle_completed_at" in kwargs
                else ("error_report" if "last_error" in kwargs else "idle")
            )
        )
        tick(name)
        if name == "completion":
            stopped.set()
        return SimpleNamespace(desired_state="running")

    def cycle():
        nonlocal cycle_count
        cycle_count += 1
        tick("cycle")
        if stage == "error_report" and cycle_count == 1:
            raise ValueError("private SDK detail")
        return {}

    runtime = SimpleNamespace(
        provider="finlab",
        drain=lambda: tick("drain"),
        cycle=cycle,
        pause=lambda: events.append("pause"),
        state=SimpleNamespace(health=lambda _: {}),
    )
    assert (
        daily.run_worker(
            runtime,
            SimpleNamespace(poll=poll),
            lambda: tick("gate") or True,
            stopped=stopped,
            run_forever=True,
            poll_seconds=3,
        )
        == 0
    )
    assert failed and events.count("drain") == 2
    assert "pause" in events
    assert cycle_count == (2 if stage in {"cycle", "completion", "error_report"} else 1)


@pytest.mark.parametrize("provider", sorted(daily._SCOPES))
@pytest.mark.parametrize("profile", ["pilot", "full_market"])
def test_actual_daily_cli_retries_initial_poll_before_runtime_creation(
    tmp_path, monkeypatch, provider, profile
):
    stopped, events = Stop(), []

    @contextmanager
    def stopping():
        yield stopped

    class Control:
        def __init__(self, config, key):
            assert key == f"{profile}_{provider}_v1"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def poll(self, **kwargs):
            events.append("poll")
            if len(events) == 1:
                raise SchedulerControlResponseError(521)
            return SimpleNamespace(
                provider=provider,
                dataset_keys=(
                    ["us_equity_eod", "hk_equity_eod"]
                    if provider == "twelve_data"
                    else daily._SCOPES[provider]
                ),
                desired_state="running",
            )

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    def runtime(**kwargs):
        events.append("runtime")
        return Client()

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account.sqlite3"))
    monkeypatch.setattr(daily, "scheduler_stop_event", stopping)
    monkeypatch.setattr(daily, "SchedulerControlClient", Control)
    monkeypatch.setattr(
        daily.FetcherConfig, "from_env", lambda: SimpleNamespace(contracts_dir=None)
    )
    monkeypatch.setattr(daily, "FullMarketSourceClient", Client)
    monkeypatch.setattr(daily, "SourceAPIClient", Client)
    monkeypatch.setattr(daily, "ContractRegistry", lambda _: None)
    monkeypatch.setattr(daily.RawStorageConfig, "from_env", lambda: None)
    monkeypatch.setattr(daily, "R2RawPayloadStore", lambda _: None)
    monkeypatch.setattr(daily, "FullMarketRuntime", runtime)
    monkeypatch.setattr(daily, "run_worker", lambda *a, **kw: 0)
    assert (
        daily.main(
            [
                "--config",
                str(Path(__file__).resolve().parents[1] / "configs/full_market.production.v1.json"),
                "--provider",
                provider,
                "--execution-profile",
                profile,
                "--state-path",
                str(tmp_path / "checkpoint.sqlite3"),
                "--run-forever",
            ]
        )
        == 0
    )
    assert events == ["poll", "poll", "runtime"]
    assert stopped.waits == [1]


@pytest.mark.parametrize("worker", ["daily", "historical"])
@pytest.mark.parametrize("fatal", [False, True])
def test_current_workers_never_hide_active_owner_recovery(worker, fatal):
    failure = (
        queue.ProviderWorkRecoveryBlockedError("fence unknown")
        if fatal
        else (FullMarketProtocolError("Source unavailable", status_code=521))
    )

    def operation(*args):
        raise failure

    if worker == "historical":
        original = historical.run_once
        historical.run_once = operation
        try:
            with pytest.raises(type(failure)):
                historical.run_worker(None, None, run_forever=fatal, stop_event=Stop())
        finally:
            historical.run_once = original
    else:
        runtime = SimpleNamespace(drain=operation, pause=lambda: None)
        with pytest.raises(type(failure)):
            daily.run_worker(
                runtime, None, lambda: True, stopped=Stop(), run_forever=fatal, poll_seconds=1
            )


def test_historical_idle_transport_recovers_and_rechecks_work(monkeypatch):
    outcomes = iter([FullMarketProtocolError("transport", retryable=True), True, False])
    stopped = Stop(after=2)
    calls = []

    def once(*args):
        calls.append(1)
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(historical, "run_once", once)
    historical.run_worker(None, None, run_forever=True, stop_event=stopped)
    assert len(calls) == 3
    assert stopped.waits == [1, 15]


SIGNAL_PROCESS = r"""
import sys
from types import SimpleNamespace
import httpx
from findb_fetcher import full_market_cli as daily, historical_backfill_cli as historical
from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketSourceClient
from findb_fetcher.scheduler_control import SchedulerControlClient
mode,config_path,state_path=sys.argv[1:]
config=FetcherConfig("https://source.example", "private",max_attempts=2 if mode.endswith("inner") else 1)
def response(request):
    print("waiting",flush=True)
    return httpx.Response(521,headers={"Retry-After":"30"},json={})
http=httpx.Client(transport=httpx.MockTransport(response))
if mode.startswith("daily"):
    real=SchedulerControlClient
    daily.SchedulerControlClient=lambda config,key:real(config,key,client=http)
    daily.FetcherConfig.from_env=lambda:config
    result=daily.main(["--config",config_path,"--state-path",state_path,"--provider","finlab","--run-forever"])
else:
    class Control:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    historical.FetcherConfig.from_env=lambda:config
    historical.HistoricalControlClient=lambda *args,**kwargs:Control()
    historical.FinLabHistoricalRunner.from_env=lambda:object()
    historical.run_once=lambda *args:FullMarketSourceClient(config,client=http).request("GET","provider-work/claim")
    result=historical.main(["--provider","finlab","--run-forever"])
http.close()
raise SystemExit(result)
"""


@pytest.mark.parametrize(
    "mode", ["daily_inner", "daily_outer", "historical_inner", "historical_outer"]
)
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_actual_clis_stop_during_inner_or_outer_outage_wait(tmp_path, mode, signum):
    env = {
        **os.environ,
        "APP_ENVIRONMENT": "production",
        "FETCHER_ACCOUNT_STATE_PATH": str(tmp_path / "account.sqlite3"),
    }
    command = [
        sys.executable,
        "-u",
        "-c",
        SIGNAL_PROCESS,
        mode,
        str(Path(__file__).resolve().parents[1] / "configs/full_market.production.v1.json"),
        str(tmp_path / "checkpoint.sqlite3"),
    ]
    with subprocess.Popen(
        command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        try:
            ready, _, _ = select.select([process.stdout], [], [], 5)
            assert ready, "CLI did not reach actual Source retry"
            assert process.stdout.readline().strip() == b"waiting"
            process.send_signal(signum)
            stdout, stderr = process.communicate(timeout=3)
            assert process.returncode == 0, stderr
            assert b"waiting" not in stdout
            assert b"private" not in stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_active_owned_client_backoff_finishes_instead_of_cancelling_drain(tmp_path):
    stopped = threading.Event()
    stopped.set()
    sleeps = []
    with owned_unit_work("finlab", tmp_path), scheduler_stop_event(stopped):
        source_retry_sleep(2, sleeps.append)
    assert sleeps == [2]


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
@pytest.mark.parametrize(
    "phase", ["initial_owner", "gate", "source_authorization", "dispatch_owner", "after_dispatch"]
)
def test_authorization_outages_preserve_only_proven_undispatched_member_budget(
    tmp_path, monkeypatch, provider, phase
):
    runtime, source, _ = _runtime(tmp_path, provider=provider)
    runtime._acquisition_allowed = lambda: True
    runtime.daily_policy = None
    plan = _plan()
    runtime.state.record_plan(plan, provider)
    key, other = "fp1:123:AAPL", "fp1:123:MSFT"
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET attempts=3 WHERE key=?", (key,))
        db.execute("INSERT INTO full_quota VALUES('old-account','old-window',11,200,99)")
    runtime.state.prepare(other, b'[{"idempotency_key":"original"}]')
    runtime.state.save_sequence_receipt(other, "original", "original-run")
    runtime.state.finish(other, status="complete", receipt="original-run")
    native, auth_calls = [], []
    try:
        with owned_unit_work(provider, tmp_path) as owner:
            owner.journal.reserve_attempt("existing-operation", 8)
            owner.journal.observe_cooldown(1)
            original = owner.source.request

            def request(method, route, body):
                if route.endswith("/authorize"):
                    auth_calls.append(route)
                    if phase == "initial_owner" or (
                        phase == "dispatch_owner" and len(auth_calls) >= 2
                    ):
                        raise FullMarketProtocolError("Source unavailable", status_code=521)
                return original(method, route, body)

            owner.source.request = request

            def allowed():
                if phase == "gate":
                    raise SchedulerControlTransportError("private control URL")
                return True

            def grant(*args, **kwargs):
                if phase == "source_authorization":
                    raise httpx.ConnectError("private Source authorization URL")
                now = time.time()
                return acq.Authorization(time.monotonic(), now, now, provider=provider)

            source.authorization = grant

            @_governed
            def acquire(self, member, frozen, *args):
                if phase == "after_dispatch":
                    with acq.Acquisition(provider=provider, credential="offline") as operation:
                        operation.start()
                        native.append("native")
                        operation.outcome = "success"
                    raise queue.ProviderWorkSourceUnavailableError("later field authorization")
                # Exercise the real authorization_scope callback after the gate.
                with acq.Acquisition(provider=provider, credential="offline") as operation:
                    if phase == "dispatch_owner":
                        operation.start()
                    else:
                        operation.authorize()
                pytest.fail("Source outage must stop before provider dispatch")

            runtime._acquire = MethodType(acquire, runtime)

            # Only the governed gate should fail; earlier member eligibility
            # checks are left available until after initial owner authorization.
            governed = runtime._acquire
            runtime._acquire = lambda *args: (
                setattr(runtime, "_acquisition_allowed", allowed),
                governed(*args),
            )[1]
            with pytest.raises(queue.ProviderWorkSourceUnavailableError):
                runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
            assert native == (["native"] if phase == "after_dispatch" else [])
            with runtime.state.connection() as db:
                row = db.execute(
                    "SELECT attempts,reason,lease_until FROM full_work WHERE key=?", (key,)
                ).fetchone()
                quota = tuple(
                    db.execute(
                        "SELECT requests,bytes,blocked_until FROM full_quota WHERE account='old-account'"
                    ).fetchone()
                )
            assert row["attempts"] == (4 if phase == "after_dispatch" else 3)
            if phase != "after_dispatch":
                assert row["reason"] == "source_unavailable" and row["lease_until"] == 0
            assert quota == (11, 200, 99)
            with runtime.state.connection() as db:
                used = db.execute(
                    "SELECT COALESCE(SUM(requests),0) FROM full_quota WHERE account<>'old-account'"
                ).fetchone()[0]
            # The same dispatched request is retained in both daily and minute windows.
            assert used == 2 * int(phase == "after_dispatch")
            with owner.journal.connection() as db:
                assert db.execute("SELECT attempts FROM work_budget").fetchone()[0] == 1
            assert owner.journal.cooldown_until() == 1
        assert source.outcomes == []
        assert runtime.state.prepared(other) == b'[{"idempotency_key":"original"}]'
        assert runtime.state.sequence_receipt(other, "original") == "original-run"
        assert runtime.state.receipt(other) == "original-run"
        restarted = FullMarketState(runtime.state.path)
        with restarted.connection() as db:
            assert (
                db.execute("SELECT attempts FROM full_work WHERE key=?", (key,)).fetchone()[0]
                == row["attempts"]
            )
    finally:
        runtime.close()


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
def test_repeated_source_outages_never_exhaust_original_member_budget(
    tmp_path, monkeypatch, provider
):
    runtime, source, _ = _runtime(tmp_path, provider=provider)
    runtime._acquisition_allowed = lambda: True
    runtime.daily_policy = None
    runtime._acquire = lambda *args: pytest.fail("Source outage dispatched native work")
    plan = _plan()
    runtime.state.record_plan(plan, provider)
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET attempts=3")
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    try:
        for _ in range(12):
            with owned_unit_work(provider, tmp_path) as owner:
                original = owner.source.request

                def unavailable(method, route, body):
                    if route.endswith("/authorize"):
                        raise FullMarketProtocolError("Source unavailable", status_code=521)
                    return original(method, route, body)

                owner.source.request = unavailable
                with pytest.raises(queue.ProviderWorkSourceUnavailableError):
                    runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
            runtime.state = FullMarketState(runtime.state.path)
            clock[0] += 61
        assert source.outcomes == []
        with runtime.state.connection() as db:
            rows = db.execute("SELECT attempts,status FROM full_work").fetchall()
            assert [tuple(row) for row in rows] == [(3, "pending"), (3, "pending")]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "error",
    [
        FullMarketProtocolError("throttled", status_code=429, retry_after=120),
        SchedulerControlResponseError(521, retry_after=120),
    ],
)
def test_outer_wait_respects_bounded_source_retry_after(error):
    stopped = Stop(after=1)

    def operation():
        raise error

    with pytest.raises(SourceWaitStoppedError):
        retry_source(operation, stopped=stopped, run_forever=True, max_delay=5)
    assert stopped.waits == [5]


def test_historical_transport_and_missing_status_protocol_errors_are_distinct():
    protocol = HistoricalControlError("invalid work identity")
    transport = HistoricalControlError("historical control transport failed")
    transport.__cause__ = httpx.ConnectError("private Source URL")
    assert not transient_source_error(protocol)
    assert transient_source_error(transport)
    assert transient_source_error(
        HistoricalControlError("authorization unavailable", status_code=503)
    )
