"""Real inherited flock descriptors and crash-safe provider response journaling."""

import multiprocessing
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from findb_fetcher import provider_work as queue
from findb_fetcher.provider_process import ProviderProcessError, supervised_call
from findb_fetcher.providers.finlab import FinLabDatasetTable
from findb_fetcher.providers.shioaji import ShioajiKbarsSnapshot
from findb_fetcher.providers.twelve_data import TwelveDataResponse
from provider_work_support import work_authorization
from provider_worker_support import fake_finlab, install_fake_sdk


def test_settlement_retains_host_lock_until_accepted_normalization_drains(tmp_path, monkeypatch):
    from findb_fetcher.full_market_client import FullMarketProtocolError

    requests = []

    class Source:
        def request(self, method, path, body):
            requests.append(path.rsplit("/", 1)[-1])
            if requests.count("settle") == 1 and path.endswith("/settle"):
                raise FullMarketProtocolError(
                    "still processing", status_code=409, code="PROVIDER_WORK_DRAINING"
                )
            return {}

    def observe_drain(_seconds):
        with pytest.raises(queue.ProviderWorkWaitingError):
            with queue.host_lock("finlab", root=tmp_path):
                pytest.fail("accepted normalization still owns the whole work")

    monkeypatch.setattr(queue.time, "sleep", observe_drain)
    with queue.host_lock("finlab", root=tmp_path):
        owner = queue.ProviderWorkSession(
            Source(),
            "finlab",
            {"work_id": "original", "generation": 1, "token": "fence"},
            queue.WorkJournal(tmp_path / "journal.sqlite3", "original"),
        )
        try:
            owner.settle("retry")
            assert owner.settled and requests == ["close", "settle", "settle"]
        finally:
            owner.stop()


def test_enforced_daily_quota_waits_do_not_publish_failure_or_consume_retry_budget(
    tmp_path, monkeypatch
):
    from findb_fetcher.full_market_state import QuotaBlockedError
    from test_full_market import _member, _plan, _runtime

    runtime, source, _ = _runtime(tmp_path)
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    now = [100.0]
    monkeypatch.setattr("findb_fetcher.full_market_runtime.time.time", lambda: now[0])
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda *args: (_ for _ in ()).throw(
        QuotaBlockedError("cooldown", quota_wait=True)
    )
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    try:
        for _ in range(12):
            assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
            now[0] += 61
        assert source.outcomes == []
        with runtime.state.connection() as db:
            row = db.execute("SELECT attempts,status,reason FROM full_work").fetchone()
            assert tuple(row) == (0, "pending", "rate_limited")
    finally:
        runtime.close()


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex", "factset"])
def test_orphan_provider_descendant_keeps_whole_work_lock_after_owner_exit(tmp_path, provider):
    release_read, release_write = os.pipe()
    child_script = "import os,sys; os.read(int(sys.argv[1]),1)"
    owner_script = """
import os,subprocess,sys
from pathlib import Path
from findb_fetcher.provider_work import host_lock,inherited_fds
with host_lock(sys.argv[1], root=Path(sys.argv[2])):
    child=subprocess.Popen([sys.executable,'-c',sys.argv[4],sys.argv[3]],pass_fds=(int(sys.argv[3]),*inherited_fds()),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print(child.pid,flush=True)
    os._exit(9)
"""
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            owner_script,
            provider,
            str(tmp_path),
            str(release_read),
            child_script,
        ],
        pass_fds=(release_read,),
        stdout=subprocess.PIPE,
        text=True,
    )
    assert owner.stdout.readline().strip().isdigit()
    assert owner.wait(timeout=5) == 9
    os.close(release_read)
    try:
        with pytest.raises(queue.ProviderWorkWaitingError):
            with queue.host_lock(provider, root=tmp_path):
                pytest.fail("orphan SDK descendant still owns the descriptor")
        with queue.host_lock("other_provider", root=tmp_path):
            pass
        os.write(release_write, b"x")
        deadline = time.monotonic() + 3
        while True:
            try:
                with queue.host_lock(provider, root=tmp_path):
                    break
            except queue.ProviderWorkWaitingError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.02)
    finally:
        os.close(release_write)


@pytest.mark.parametrize(
    "value",
    [
        TwelveDataResponse({"values": [{"close": "1"}]}, raw_bytes=b'{"values":[{"close":"1"}]}'),
        FinLabDatasetTable(("2026-10-15",), ("2330",), ((1, None),)),
        ShioajiKbarsSnapshot({"ts": (1,), "Close": (2,)}, 10, 20),
        b"native-taifex-report\r\n",
    ],
)
def test_all_provider_native_responses_survive_raw_upload_crash_and_retry(tmp_path, value):
    calls = []

    @queue.durable_response("provider", ("day",))
    def acquire(day):
        calls.append(day)
        return value

    journal_path = tmp_path / "work.sqlite3"
    token = queue._current.set(
        SimpleNamespace(journal=queue.WorkJournal(journal_path, "stable-original"))
    )
    try:
        first = acquire("2026-10-15")
        # Simulate raw upload/preparation failure and a new queue attempt/process.
        queue._current.set(
            SimpleNamespace(journal=queue.WorkJournal(journal_path, "stable-original"))
        )
        resumed = acquire("2026-10-15")
        assert calls == ["2026-10-15"]
        assert resumed == first
        if isinstance(value, TwelveDataResponse):
            assert resumed.raw_bytes == value.raw_bytes
        queue._current.set(
            SimpleNamespace(journal=queue.WorkJournal(journal_path, "distinct-request"))
        )
        acquire("2026-10-15")
        assert len(calls) == 2
    finally:
        queue._current.reset(token)


def test_prepared_bytes_cannot_change_when_receipt_was_lost(tmp_path):
    journal = queue.WorkJournal(tmp_path / "work.sqlite3", "original")
    journal.prepare("sequence:1", b'{"immutable":1}')
    journal.prepare("sequence:2", b'{"immutable":2}')
    restarted = queue.WorkJournal(journal.path, "original")
    restarted.prepare("sequence:1", b'{"immutable":1}')
    with pytest.raises(queue.ProviderWorkRecoveryBlockedError, match="bytes changed"):
        restarted.prepare("sequence:1", b'{"immutable":2}')


def test_blocking_sdk_timeout_terminates_and_reaps_actual_process(tmp_path, monkeypatch):
    launched = install_fake_sdk(monkeypatch, tmp_path / "sdk", fake_finlab("time.sleep(30)"))
    with pytest.raises(ProviderProcessError):
        supervised_call(
            "finlab.get",
            {"token": "offline", "dataset": "slow", "symbols": ("2330",), "account": False},
            timeout=0.05,
        )
    assert [process.poll() is not None for process in launched] == [True]


def test_persistent_shioaji_supervisor_reaps_real_isolated_process_group(tmp_path):
    from findb_fetcher.providers.shioaji_session import (
        PersistentIsolatedShioajiGateway,
        _ProviderProcess,
    )

    with queue.host_lock("shioaji", root=tmp_path):
        parent, child = multiprocessing.Pipe()
        gateway = PersistentIsolatedShioajiGateway.__new__(PersistentIsolatedShioajiGateway)
        gateway.parent, gateway.process = parent, _ProviderProcess(child.fileno())
        child.close()
        gateway._stop()
        assert gateway.process.child.poll() is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(gateway.process.pid, 0)
    with queue.host_lock("shioaji", root=tmp_path):
        pass


def test_host_lock_is_quarantined_when_child_stop_cannot_be_proven(tmp_path):
    with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
        with queue.host_lock("shioaji", root=tmp_path):
            raise queue.ProviderWorkRecoveryBlockedError("unreaped child")
    with pytest.raises(queue.ProviderWorkWaitingError):
        with queue.host_lock("shioaji", root=tmp_path):
            pytest.fail("uncertain process death cannot advance work")


@pytest.mark.parametrize("status", [400, 401, 403, 422, 429, 503])
def test_common_source_rejection_classification_preserves_actual_status_identity_and_digest(
    tmp_path, monkeypatch, status
):
    import hashlib
    from pathlib import Path

    import httpx

    from findb_fetcher.client import (
        PreparedDelivery,
        SourceAPIClient,
        SourceAPIResponseError,
        permanent_source_failure,
    )
    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.contracts import ContractRegistry

    class Source:
        def request(self, method, path, body):
            return {}

    body = b'{"immutable":"original"}'
    prepared = PreparedDelivery(body, "original-key", "tw_equity_eod", "market_eod", 1)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status, json={"error": {"code": "DATASET_ACCESS_DENIED"}}, request=request
        )
    )
    config = FetcherConfig(
        "https://source.test",
        "offline",
        contracts_dir=Path(__file__).resolve().parents[2] / "contracts",
        max_attempts=1,
    )
    owner = queue.ProviderWorkSession(
        Source(),
        "finlab",
        {"work_id": "frozen", "generation": 1, "token": "fence"},
        queue.WorkJournal(tmp_path / "journal.sqlite3", "frozen"),
    )
    token = queue._current.set(owner)
    try:
        with SourceAPIClient(
            config, ContractRegistry(config.contracts_dir), client=httpx.Client(transport=transport)
        ) as client:
            with pytest.raises(SourceAPIResponseError) as failure:
                client.deliver(prepared)
        assert permanent_source_failure(failure.value) == (status in {400, 401, 403, 422})
        assert owner.journal.latest_source_error() == {
            "delivery_key": "original-key",
            "http_status": status,
            "code": "DATASET_ACCESS_DENIED",
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
    finally:
        queue._current.reset(token)
        owner.stop()


def test_observed_cooldown_survives_restart_fences_new_io_and_sets_tail_deadline(tmp_path):
    from datetime import datetime, timezone

    from findb_fetcher.full_market_state import QuotaBlockedError

    requests = []

    class Source:
        def request(self, method, path, body):
            requests.append((path.rsplit("/", 1)[-1], body))
            return (
                work_authorization("twelve_data", body, work_id="original-work")
                if path.endswith("/authorize")
                else {}
            )

    path = tmp_path / "journal.sqlite3"
    original = queue.WorkJournal(path, "original-work")
    original.prepare("immutable", b"original prepared bytes")
    deadline = time.time() + 120
    original.observe_cooldown(deadline)
    original.observe_cooldown(deadline - 60)
    reopened = queue.WorkJournal(path, "original-work")
    owner = queue.ProviderWorkSession(
        Source(),
        "twelve_data",
        {"work_id": "original-work", "generation": 2, "token": "recovered-fence"},
        reopened,
    )
    try:
        with pytest.raises(QuotaBlockedError) as wait:
            owner.authorize()
        assert wait.value.quota_wait and not wait.value.dispatched
        assert requests[0][0] == "authorize", "fencing must precede a cooldown wait"
        assert reopened.cooldown_until() == deadline
        owner.settle("retry")
        retry_at = datetime.fromisoformat(requests[-1][1]["retry_at"])
        assert (retry_at - datetime.now(timezone.utc)).total_seconds() > 110
        with reopened.connection() as db:
            assert (
                db.execute(
                    "SELECT body FROM work_delivery WHERE work_key=? AND member_key=?",
                    ("original-work", "immutable"),
                ).fetchone()[0]
                == b"original prepared bytes"
            )
        owner.failed.set()
        with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
            owner.authorize()
    finally:
        owner.stop()


@pytest.mark.parametrize("legacy_valid", [False, True])
def test_target_validated_cache_preserves_legacy_bytes_prepared_and_budget_on_upgrade(
    tmp_path, legacy_valid
):
    import base64
    import hashlib
    import json

    target = "2026-10-15"
    legacy_raw = (
        b'{"values":[{"datetime":"2026-10-15"}]}'
        if legacy_valid
        else b'{"values":[{"datetime":"2026-10-14"}]}'
    )
    legacy = json.dumps(
        {
            "type": "TwelveDataResponse",
            "data": {
                "payload": json.loads(legacy_raw),
                "raw_bytes": base64.b64encode(legacy_raw).decode(),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    key = (
        "target:"
        + hashlib.sha256(
            json.dumps({"day": target}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    path = tmp_path / "journal.sqlite3"
    journal = queue.WorkJournal(path, "original")
    journal.response(key, legacy)
    journal.prepare("immutable-member", b"original prepared body")
    journal.reserve_attempt("original-operation", 3)
    journal.reserve_attempt("original-operation", 3)
    calls = []

    @queue.durable_response(
        "target",
        ("day",),
        target_valid=lambda value, args: value["values"][0]["datetime"] == args["day"],
    )
    def fetch(day):
        calls.append(day)
        raw = b'{"values":[{"datetime":"2026-10-15"}]}'
        return TwelveDataResponse(json.loads(raw), raw_bytes=raw)

    token = queue._current.set(SimpleNamespace(journal=journal))
    try:
        first = fetch(target)
        queue._current.set(SimpleNamespace(journal=queue.WorkJournal(path, "original")))
        replay = fetch(target)
        assert first.raw_bytes == replay.raw_bytes
        assert calls == ([] if legacy_valid else [target])
        assert journal.response(key) == legacy
        with journal.connection() as db:
            negative = db.execute(
                "SELECT body,sha256,origin FROM work_response_observation"
            ).fetchall()
            assert negative == (
                []
                if legacy_valid
                else [(legacy, hashlib.sha256(legacy).hexdigest(), "legacy_cache")]
            )
            assert (
                db.execute("SELECT body FROM work_delivery").fetchone()[0]
                == b"original prepared body"
            )
            assert db.execute("SELECT attempts FROM work_budget").fetchone()[0] == 2
    finally:
        queue._current.reset(token)


def test_target_validation_requires_requested_end_date_not_an_earlier_row_in_window():
    from datetime import date

    from findb_fetcher.providers.twelve_data import _target_rows_present

    arguments = {"start_date": date(2026, 10, 13), "end_date": date(2026, 10, 15)}
    assert not _target_rows_present({"values": [{"datetime": "2026-10-14"}]}, arguments)
    assert _target_rows_present(
        {"values": [{"datetime": "2026-10-14"}, {"datetime": "2026-10-15"}]}, arguments
    )
