"""Observe actual invocations, crash recovery and immutable authorization origins."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from multiprocessing.reduction import DupFd

import httpx
import pytest

from findb_fetcher.acquisition import Acquisition, Authorization, authorization_scope
from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.platform_limits import account_identity
from findb_fetcher.providers.shioaji_control import usage_sample
from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig
from provider_work_support import account_boundary_unit
from sdk_bootstrap_harness import SDK_CHILD as _FAKE_PROVIDER_CHILD


def _grant() -> Authorization:
    now = time.time()
    return Authorization(time.monotonic(), now, now)


@pytest.fixture
def governor(tmp_path, monkeypatch):
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "shared-key")
    monkeypatch.setenv("SHIOAJI_API_KEY", "shared-shioaji")
    return path


@account_boundary_unit
def _worker(path: str, queue, count: int = 2) -> None:
    os.environ["FETCHER_ACCOUNT_STATE_PATH"] = path
    os.environ["TWELVE_DATA_API_KEY"] = "shared-key"
    with authorization_scope(_grant):
        for _ in range(count):
            with Acquisition("twelve_data") as operation:
                operation.start()
                queue.put(("start", time.time()))
                time.sleep(0.025)
                queue.put(("complete", time.time()))


@account_boundary_unit
def _crash(path: str, queue) -> None:
    os.environ["FETCHER_ACCOUNT_STATE_PATH"] = path
    os.environ["TWELVE_DATA_API_KEY"] = "shared-key"
    with authorization_scope(_grant), Acquisition("twelve_data") as operation:
        operation.prepare()
        queue.put(operation.operation_id)
        queue.close()
        queue.join_thread()
        os._exit(9)


@account_boundary_unit
def _headers_then_body(path: str, queue) -> None:
    os.environ["FETCHER_ACCOUNT_STATE_PATH"] = path
    os.environ["TWELVE_DATA_API_KEY"] = "shared-key"

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            queue.put("body")
            while True:
                time.sleep(1)
            yield b""

    with (
        authorization_scope(_grant),
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(429, headers={"Retry-After": "200"}, stream=Body())
            )
        ) as http,
    ):
        TwelveDataClient(TwelveDataConfig(api_key="shared-key"), client=http).fetch_daily("AAPL")


def _sdk_child(conn, queue, delay: float, work_descriptor):
    import json

    from findb_fetcher.providers.shioaji_control import ChildBoundary

    command = json.loads(conn.recv_bytes(4096))
    work_fd = work_descriptor.detach()
    command["governor"]["work_fd"] = work_fd
    boundary = ChildBoundary(conn, command["governor"])

    class API:
        def usage(self):
            return {
                "bytes": 10,
                "limit_bytes": 524288000,
                "remaining_bytes": 524287990,
                "connections": 1,
            }

    api = API()
    try:
        boundary.start(api)
        queue.put(("kbars", time.time()))
        time.sleep(delay)
        boundary.finish(api)
        queue.put(("done", time.time()))
        conn.send_bytes(
            json.dumps(
                {"stage": "result", "actual_start": boundary.started_at, "payload": {}}
            ).encode()
        )
    finally:
        boundary.close()
        conn.close()


@account_boundary_unit
def _sdk_parent(path: str, queue):
    from findb_fetcher.providers.shioaji_control import parent_fetch

    os.environ["FETCHER_ACCOUNT_STATE_PATH"] = path
    os.environ["SHIOAJI_API_KEY"] = "shared-shioaji"
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(
        target=_sdk_child,
        args=(
            child,
            queue,
            0.4,
            DupFd(
                __import__(
                    "findb_fetcher.provider_work", fromlist=["inherited_fds"]
                ).inherited_fds()[0]
            ),
        ),
    )
    process.start()
    child.close()
    with authorization_scope(_grant), Acquisition("shioaji") as operation:
        parent_fetch(parent, process, {"op": "test"}, operation, timeout=5, max_bytes=4096)
    process.join(5)


def test_parent_kill_keeps_child_lock_until_known_sdk_finalization(governor):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    parent = ctx.Process(target=_sdk_parent, args=(str(governor), queue))
    parent.start()
    kind, start = queue.get(timeout=10)
    assert kind == "kbars"
    parent.kill()
    parent.join(10)
    with authorization_scope(_grant), Acquisition("shioaji"):
        # Constructor/entry must wait for the surviving child's locked descriptor.
        assert time.time() - start >= 0.35
    assert queue.get(timeout=10)[0] == "done"
    with FullMarketState(governor).connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM acquisition_intent WHERE status='finalized'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute("SELECT COUNT(*) FROM acquisition_intent WHERE status='unknown'").fetchone()[
                0
            ]
            == 0
        )


def test_expired_child_go_refreshes_without_duplicate_sdk_start(governor):
    from findb_fetcher.providers.shioaji_control import parent_fetch

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    parent, child = ctx.Pipe()
    process = ctx.Process(
        target=_sdk_child,
        args=(
            child,
            queue,
            0,
            DupFd(
                __import__(
                    "findb_fetcher.provider_work", fromlist=["inherited_fds"]
                ).inherited_fds()[0]
            ),
        ),
    )
    process.start()
    child.close()
    calls = []

    def authorize():
        grant = _grant()
        calls.append(grant)
        if len(calls) == 1:
            time.sleep(0.06)
        return grant

    with authorization_scope(authorize), Acquisition("shioaji") as operation:
        parent_fetch(parent, process, {"op": "test"}, operation, timeout=5, max_bytes=4096)
    process.join(5)
    parent.close()
    assert process.exitcode == 0
    assert len(calls) == 2
    assert [queue.get(timeout=5)[0] for _ in range(2)] == ["kbars", "done"]


def test_processes_space_actual_starts_from_confirmed_completion(governor):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    workers = [ctx.Process(target=_worker, args=(str(governor), queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    events = [queue.get(timeout=10) for _ in range(8)]
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    events.sort(key=lambda event: event[1])
    assert [event[0] for event in events] == ["start", "complete"] * 4
    assert all(events[index + 1][1] - events[index][1] >= 60 / 1597 - 0.002 for index in (1, 3, 5))
    with FullMarketState(governor).connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM acquisition_intent WHERE status='finalized'"
            ).fetchone()[0]
            == 4
        )
        assert (
            sum(
                row[0]
                for row in db.execute("SELECT requests FROM full_quota WHERE length(window)=10")
            )
            == 4
        )


def test_threads_serialize_same_account_and_other_account_remains_free(governor, monkeypatch):
    from threading import Event

    entered, release = Event(), Event()

    def slow():
        with authorization_scope(_grant), Acquisition("twelve_data") as operation:
            operation.start()
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(slow)
        assert entered.wait(5)
        began = time.monotonic()
        with authorization_scope(_grant), Acquisition("finlab") as operation:
            operation.start()
        assert time.monotonic() - began < 1
        release.set()
        future.result(timeout=5)


def test_delayed_durable_prepare_then_revocation_spends_no_request(governor, monkeypatch):
    original = Acquisition.prepare
    stopped = []

    def delayed(self):
        original(self)
        stopped.append(True)
        time.sleep(0.06)

    monkeypatch.setattr(Acquisition, "prepare", delayed)

    def denied():
        assert stopped
        raise QuotaBlockedError("revoked")

    with authorization_scope(denied), pytest.raises(QuotaBlockedError, match="revoked"):
        with Acquisition("twelve_data") as operation:
            operation.start()
            pytest.fail("provider must not start")
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0
        assert db.execute("SELECT status FROM acquisition_intent").fetchone()[0] == "cancelled"


def test_armed_process_death_records_one_unknown_and_full_cooldown(governor):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    worker = ctx.Process(target=_crash, args=(str(governor), queue))
    worker.start()
    identity = queue.get(timeout=10)
    worker.join(10)
    assert worker.exitcode == 9
    began = time.time()
    with Acquisition("twelve_data") as operation:
        operation.recover()
    with FullMarketState(governor).connection() as db:
        row = db.execute(
            "SELECT * FROM acquisition_intent WHERE operation_id=?", (identity,)
        ).fetchone()
        assert row["status"] == "unknown" and row["actual_start"] is None
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] >= began + 300
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM acquisition_intent WHERE status='unknown'").fetchone()[
                0
            ]
            == 1
        )


def test_kill_after_429_headers_keeps_early_cooldown_and_unknown_liability(governor):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    worker = ctx.Process(target=_headers_then_body, args=(str(governor), queue))
    worker.start()
    assert queue.get(timeout=10) == "body"
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] > time.time() + 190
    worker.kill()
    worker.join(10)
    now = time.time()
    with Acquisition("twelve_data"):
        pass
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] >= now + 300
        assert (
            db.execute("SELECT COUNT(*) FROM acquisition_intent WHERE status='unknown'").fetchone()[
                0
            ]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0


def _response(*, checked_at: float | None = None, stream=None):
    stamp = datetime.fromtimestamp(checked_at or time.time(), timezone.utc).isoformat()
    body = {
        "acquisition_allowed": True,
        "provider": "twelve_data",
        "checked_at": stamp,
        "max_age_seconds": 5,
    }
    return (
        httpx.Response(200, headers={"X-FinDB-Acquisition-Checked-At": stamp}, json=body)
        if stream is None
        else httpx.Response(
            200, headers={"X-FinDB-Acquisition-Checked-At": stamp}, stream=stream(stamp)
        )
    )


def test_body_decode_pause_does_not_refresh_receipt_origin():
    import json

    class SlowBody(httpx.SyncByteStream):
        def __init__(self, stamp):
            self.stamp = stamp

        def __iter__(self):
            time.sleep(0.06)
            yield json.dumps(
                {
                    "acquisition_allowed": True,
                    "provider": "twelve_data",
                    "checked_at": self.stamp,
                    "max_age_seconds": 5,
                }
            ).encode()

    with httpx.Client(transport=httpx.MockTransport(lambda _: _response(stream=SlowBody))) as http:
        grant = FullMarketSourceClient(
            FetcherConfig("https://source.example", "safe"), client=http
        ).authorization("GET", "test")
    assert not grant.valid()
    assert grant.deadline() <= grant.origin + 0.050


def test_realistic_source_latency_is_allowed_but_old_server_check_is_rejected():
    def response(_):
        checked = time.time()
        time.sleep(1)
        return _response(checked_at=checked)

    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        assert (
            FullMarketSourceClient(FetcherConfig("https://source.example", "safe"), client=http)
            .authorization("GET", "test")
            .valid()
        )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: _response(checked_at=time.time() - 6))
    ) as http:
        with pytest.raises(FullMarketProtocolError, match="stale"):
            FullMarketSourceClient(
                FetcherConfig("https://source.example", "safe"), client=http
            ).authorization("GET", "test")


def test_lease_and_minute_bound_cap_grant_even_before_50ms():
    assert not Authorization(time.monotonic(), time.time(), time.time(), time.time() - 1).valid()
    grant = Authorization(100, 120.999, 120.999)
    assert grant.deadline() == 100.05
    assert Authorization(100, 179.999, 179.999).deadline() == pytest.approx(100.001)


def test_sdk_counter_samples_preserve_reset_and_unavailable_is_rejected(governor):
    class API:
        def __init__(self, value):
            self.value = value

        def usage(self):
            return {
                "bytes": self.value,
                "limit_bytes": 524288000,
                "remaining_bytes": 524288000 - self.value,
            }

    with authorization_scope(_grant), Acquisition("shioaji") as operation:
        before, after_reset = usage_sample(API(1234)), usage_sample(API(10))
        operation.usage(before)
        operation.usage(after_reset)
        operation.start()
    with FullMarketState(governor).connection() as db:
        assert [
            row[0] for row in db.execute("SELECT bytes FROM acquisition_usage ORDER BY observed_at")
        ] == [1234, 10]
        assert all(row[0] == 0 for row in db.execute("SELECT bytes FROM full_quota"))
    with pytest.raises(QuotaBlockedError, match="unavailable"):
        usage_sample(type("API", (), {"usage": lambda _: {"bytes": 1}})())


def test_proven_whitespace_identity_merges_debt_once_without_current_byte_gate(
    governor, monkeypatch
):
    raw, normalized = " shared-shioaji ", "shared-shioaji"
    state = FullMarketState(governor)
    with state.connection() as db:
        for key, account, requests, byte_count in (
            (raw, "legacy-raw", 3, 600000000),
            (normalized, "legacy-normal", 4, 700000000),
        ):
            db.execute(
                "INSERT INTO full_account_identity VALUES('shioaji',?,?)",
                (hashlib.sha256(key.encode()).hexdigest(), account),
            )
            db.execute(
                "INSERT INTO full_quota(account,window,requests,bytes) VALUES(?,?,?,?)",
                (account, "2026-10-01", requests, byte_count),
            )
        db.execute("INSERT INTO full_pacing VALUES('legacy-raw',1234)")
    monkeypatch.setenv("SHIOAJI_API_KEY", raw)
    assert account_identity("shioaji", state) == "legacy-normal"
    assert account_identity("shioaji", state) == "legacy-normal"
    with state.connection() as db:
        assert tuple(
            db.execute(
                "SELECT requests,bytes FROM full_quota WHERE account='legacy-normal'"
            ).fetchone()
        ) == (7, 1300000000)
        assert db.execute("SELECT COUNT(*) FROM account_alias_audit").fetchone()[0] == 1
        assert (
            db.execute("SELECT next_at FROM full_pacing WHERE account='legacy-normal'").fetchone()[
                0
            ]
            == 1234
        )
    with authorization_scope(_grant), Acquisition("shioaji") as operation:
        # Historic incomparable scalar bytes never masquerade as current SDK traffic.
        operation.start()


def test_response_hook_pause_preserves_header_origin_and_denies_actual_start(governor):
    calls = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: _response()),
        event_hooks={"response": [lambda _: time.sleep(0.06)]},
    ) as http:
        source = FullMarketSourceClient(
            FetcherConfig("https://source.example", "safe"), client=http
        )
        with authorization_scope(lambda: source.authorization("GET", "test")):
            with Acquisition("twelve_data") as operation:
                with pytest.raises(QuotaBlockedError, match="latency"):
                    operation.start()
                    calls.append("provider")
    assert calls == []
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0


def test_unsupported_provider_transport_fails_before_contact(governor):
    calls = []

    class Unsupported(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append(request)
            return httpx.Response(200, json={})

    with httpx.Client(transport=Unsupported()) as http:
        with authorization_scope(_grant), pytest.raises(QuotaBlockedError, match="transport"):
            TwelveDataClient(TwelveDataConfig(api_key="shared-key"), client=http).fetch_daily(
                "AAPL"
            )
    assert calls == []
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0


def test_rollover_after_authorization_rechecks_and_charges_actual_day(governor, monkeypatch):
    current = [1767225599.999]
    monkeypatch.setattr(time, "time", lambda: current[0])
    calls = []

    def authorize():
        grant = _grant()
        calls.append(grant)
        if len(calls) == 1:
            current[0] += 1
        return grant

    with authorization_scope(authorize), Acquisition("twelve_data") as operation:
        operation.start()
        assert operation.started_at == current[0]
    assert len(calls) == 2
    with FullMarketState(governor).connection() as db:
        assert [row[0] for row in db.execute("SELECT window FROM full_quota ORDER BY window")] == [
            "2026-01-01",
            "2026-01-01T00:00",
        ]


def _legacy_worker(path, account, queue):
    state = FullMarketState(__import__("pathlib").Path(path))
    # This is the unchanged legacy grant algorithm, which never takes the v2 lock.
    queue.put(
        state.reserve_acquisition(
            account=account,
            interval=0,
            requests=1,
            byte_count=0,
            request_limit=100,
            byte_limit=None,
            minute_limit=100,
        )[0]
    )


def test_mixed_legacy_protocol_bypasses_lock_and_rollback_inspection_denies(
    governor, capsys, monkeypatch
):
    from findb_fetcher.account_governor import main

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    with Acquisition("twelve_data") as operation:
        operation.prepare()
        worker = ctx.Process(target=_legacy_worker, args=(str(governor), operation.account, queue))
        worker.start()
        assert queue.get(timeout=10) == 0
        worker.join(10)
        assert worker.exitcode == 0
        monkeypatch.setattr(
            "sys.argv",
            [
                "governor",
                "inspect-acquisition",
                "--governor",
                str(governor),
                "--provider",
                "twelve_data",
            ],
        )
        main()
        assert __import__("json").loads(capsys.readouterr().out)["legacy_rollback_safe"] is False
        operation.possibly_started = True
    main()
    assert __import__("json").loads(capsys.readouterr().out)["legacy_rollback_safe"] is False


@account_boundary_unit
def _launch_parent(path, output, queue):
    import subprocess
    import sys

    from findb_fetcher.providers import shioaji_session

    original = subprocess.Popen

    def launch(args, **kwargs):
        assert "SOURCE_CLIENT_KEY" not in kwargs["env"]
        assert not any("source-secret-marker" in value for value in args)
        return original(
            [sys.executable, "-c", _FAKE_PROVIDER_CHILD, args[-1], output, "kbars-slow"], **kwargs
        )

    shioaji_session.subprocess.Popen = launch
    os.environ["FETCHER_ACCOUNT_STATE_PATH"] = path
    os.environ["SHIOAJI_API_KEY"] = "shared-shioaji"
    os.environ["SOURCE_CLIENT_KEY"] = "source-secret-marker"
    os.environ["TWELVE_DATA_API_KEY"] = "other-provider-marker"
    os.environ["FINLAB_API_TOKEN"] = "other-provider-marker"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "cloud-marker"
    gateway = shioaji_session.PersistentIsolatedShioajiGateway("shared-shioaji", "provider-secret")
    queue.put(gateway.process.pid)
    with authorization_scope(_grant):
        assert len(gateway.catalogue()) == 2
        gateway.fetch_kbars("2330", __import__("datetime").date(2026, 1, 1))
    gateway.close()


def test_real_allowlisted_launch_excludes_secrets_and_retains_fd_after_parent_kill(
    governor, tmp_path
):
    import json

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    output = tmp_path / "child-evidence.jsonl"
    parent = ctx.Process(target=_launch_parent, args=(str(governor), str(output), queue))
    parent.start()
    queue.get(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        rows = (
            [json.loads(line) for line in output.read_text().splitlines()]
            if output.exists()
            else []
        )
        if any("start" in row for row in rows):
            break
        time.sleep(0.01)
    else:
        pytest.fail("SDK boundary did not start")
    assert rows[0]["environment_present"] == []
    start = next(row["start"] for row in rows if "start" in row)
    parent.kill()
    parent.join(10)
    with Acquisition("shioaji"):
        assert time.time() - start >= 0.35
    with FullMarketState(governor).connection() as db:
        assert (
            db.execute(
                "SELECT status FROM acquisition_intent WHERE operation_kind='kbars'"
            ).fetchone()[0]
            == "finalized"
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM acquisition_intent WHERE operation_kind='sdk_login' AND login_units=1"
            ).fetchone()[0]
            == 1
        )
