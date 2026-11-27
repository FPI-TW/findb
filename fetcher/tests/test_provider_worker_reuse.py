"""Reused provider workers (#753): one process serves many calls, never past its scope."""

import os
import signal

import pytest

from findb_fetcher import provider_process
from findb_fetcher import provider_work as queue
from findb_fetcher.provider_process import ProviderProcessError, close_reusable, supervised_call
from provider_worker_support import fake_finlab, install_fake_sdk

PID = """
if dataset == "fail":
    raise RuntimeError("provider words that must not leave the worker")
return os.getpid()
"""


@pytest.fixture
def launched(monkeypatch, tmp_path):
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", fake_finlab(PID))


def _pid(dataset="pid", **kwargs):
    value, _, _ = supervised_call(
        "finlab.get",
        {"token": "offline", "dataset": dataset, "symbols": ("2330",), "account": False},
        **kwargs,
    )
    return value


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_reused_worker_answers_the_next_call_and_ends_when_closed(launched):
    first = _pid(reuse=True)
    second = _pid(reuse=True)

    assert first == second and len(launched) == 1
    # Waiting for the next call, still alive.
    assert launched[0].poll() is None
    close_reusable()
    assert launched[0].poll() is not None


def test_without_reuse_every_call_gets_a_fresh_reaped_process(launched):
    assert _pid() != _pid()
    assert [process.poll() is not None for process in launched] == [True, True]


def test_a_failed_call_reaps_the_reused_worker_and_the_next_starts_fresh(launched):
    first = _pid(reuse=True)
    with pytest.raises(ProviderProcessError) as failed:
        _pid("fail", reuse=True)

    assert failed.value.code == "sdk_error"
    assert launched[0].poll() is not None and not _alive(first)
    assert _pid(reuse=True) != first


def test_a_worker_retires_after_its_call_limit(launched, monkeypatch):
    monkeypatch.setattr(provider_process, "REUSE_CALL_LIMIT", 2)

    pids = [_pid(reuse=True) for _ in range(3)]

    assert pids[0] == pids[1] != pids[2]
    assert launched[0].poll() is not None


def test_idle_workers_beyond_the_pool_size_are_reaped(launched, monkeypatch):
    monkeypatch.setattr(provider_process, "REUSE_POOL_SIZE", 0)

    _pid(reuse=True)

    assert launched[0].poll() is not None


def test_the_lane_lock_is_free_again_once_its_claim_ends(launched, tmp_path):
    with queue.host_lock("finlab", root=tmp_path):
        worker = _pid(reuse=True)
        assert _alive(worker)
    # The worker inherited the lock descriptor; it is gone before the lock closes,
    # so the lane's next claim takes the lock at once.
    assert not _alive(worker)
    with queue.host_lock("finlab", root=tmp_path):
        assert _pid(reuse=True) != worker


def test_a_worker_never_serves_another_descriptor(launched, tmp_path):
    with queue.host_lock("finlab", lane="intraday", root=tmp_path):
        inside = _pid(reuse=True)
        pool = list(provider_process._idle)
        # Simulate a worker left idle by another scope: it must not be borrowed.
        provider_process._idle.clear()
    provider_process._idle.extend(pool)
    try:
        outside = _pid(reuse=True)
    finally:
        for worker in pool:
            if worker.process.poll() is None:
                os.killpg(worker.process.pid, signal.SIGKILL)
                worker.process.wait(5)
    assert outside != inside


def test_a_worker_whose_parent_goes_away_exits_on_its_own(launched):
    _pid(reuse=True)
    [worker] = provider_process._idle
    provider_process._idle.clear()
    # The parent's end of the pipe closing is what a parent's death looks like.
    worker.conn.close()

    assert worker.process.wait(10) == 0
