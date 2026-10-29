"""Spawned account harnesses must select their own path before entering workers."""

import multiprocessing
import os
from pathlib import Path

import pytest

from provider_work_support import account_boundary_unit


@account_boundary_unit
def _path_worker(path, connection):
    # The decorator runs before any worker-side environment setup.
    from findb_fetcher.provider_work import inherited_fds

    (fd,) = inherited_fds()
    account_path = Path(path)
    (lock,) = account_path.parent.glob("provider-work-*-unit-*.lock")
    info = os.fstat(fd)
    connection.send(
        (
            os.environ.get("FETCHER_ACCOUNT_STATE_PATH"),
            (info.st_dev, info.st_ino) == (lock.stat().st_dev, lock.stat().st_ino),
        )
    )
    connection.close()


@pytest.mark.parametrize("parent_path", [None, "wrong-parent-ledger"])
def test_spawned_worker_uses_explicit_test_path_before_environment_setup(
    tmp_path, monkeypatch, parent_path
):
    wrong = tmp_path / "wrong" / "governor.sqlite3"
    if parent_path is None:
        monkeypatch.delenv("FETCHER_ACCOUNT_STATE_PATH", raising=False)
    else:
        monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(wrong))
    selected = tmp_path / "worker" / "governor.sqlite3"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    worker = context.Process(target=_path_worker, args=(str(selected), child))
    worker.start()
    child.close()
    try:
        assert parent.poll(10), "worker did not report its lock identity"
        assert parent.recv() == (str(selected), True)
        worker.join(5)
        assert worker.exitcode == 0
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        parent.close()
    assert not wrong.parent.exists()
    assert os.getenv("FETCHER_ACCOUNT_STATE_PATH") == (str(wrong) if parent_path else None)
