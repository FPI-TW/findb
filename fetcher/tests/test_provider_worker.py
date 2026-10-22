"""Exec'd provider worker: allowlist, descriptor scope, secrets and lock lifetime."""

import multiprocessing
import os
import threading
import time
import warnings

import pytest

from findb_fetcher import provider_work as queue
from findb_fetcher.provider_process import ProviderProcessError, supervised_call
from provider_worker_support import fake_finlab, install_fake_sdk

DESCRIPTORS = """
import os
import stat as stat_module

identities = []
for name in os.listdir("/dev/fd"):
    try:
        status = os.fstat(int(name))
    except OSError:
        continue
    if stat_module.S_ISREG(status.st_mode):
        identities.append((status.st_dev, status.st_ino))
return identities
"""


def _call(dataset, **kwargs):
    return supervised_call("finlab.get", {"token": "offline", "dataset": dataset}, **kwargs)


def test_unknown_operation_fails_closed_and_reaps(tmp_path, monkeypatch):
    launched = install_fake_sdk(monkeypatch, tmp_path / "sdk", fake_finlab("return 'unexpected'"))
    with pytest.raises(ProviderProcessError):
        supervised_call("finlab.login", {"token": "offline"})
    assert [process.poll() is not None for process in launched] == [True]


def test_worker_inherits_only_pipe_and_whole_work_descriptor(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch, tmp_path / "sdk", fake_finlab(DESCRIPTORS))
    leaked = os.open(tmp_path / "leaked", os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(leaked, True)
    try:
        with queue.host_lock("finlab", root=tmp_path) as (fd, _):
            lock = os.fstat(fd)
            identities = set(_call("descriptors"))
        status = os.fstat(leaked)
    finally:
        os.close(leaked)
    assert (lock.st_dev, lock.st_ino) in identities
    assert (status.st_dev, status.st_ino) not in identities


def test_credentials_stay_out_of_worker_argv_and_environment(tmp_path, monkeypatch):
    launched = install_fake_sdk(
        monkeypatch,
        tmp_path / "sdk",
        fake_finlab("return {'argv': sys.argv, 'environment': dict(os.environ)}"),
    )
    monkeypatch.setenv("SOURCE_CLIENT_KEY", "source-secret-marker")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-marker")
    monkeypatch.setenv("FINLAB_API_TOKEN", "installed-marker")
    observed = supervised_call("finlab.get", {"token": "worker-secret-marker", "dataset": "x"})
    # Only the SDK's own login installs the token, after the worker started.
    environment = observed["environment"]
    assert environment.pop("FINLAB_API_TOKEN") == "worker-secret-marker"
    assert set(environment) <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "PROVIDER_WORK_REQUIRED",
        "PYTHONPATH",
        # Set by the interpreter itself (PEP 538 C-locale coercion, macOS).
        "LC_CTYPE",
        "__CF_USER_TEXT_ENCODING",
    }
    assert "marker" not in repr(environment)
    assert "marker" not in repr([observed["argv"], launched[0].args])


def test_multithreaded_parent_launches_worker_without_fork_warning(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch, tmp_path / "sdk", fake_finlab("return 'completed'"))
    stop = threading.Event()
    heartbeat = threading.Thread(target=stop.wait, daemon=True)
    heartbeat.start()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert _call("x") == "completed"
    finally:
        stop.set()
        heartbeat.join()


def _killed_parent(root, sdk, started):
    import pytest

    from findb_fetcher import provider_process
    from findb_fetcher import provider_work as queue

    with pytest.MonkeyPatch.context() as monkeypatch:
        install_fake_sdk(
            monkeypatch,
            sdk,
            fake_finlab(f"open({started!r}, 'w').close()\ntime.sleep(3)\nreturn 'late'"),
        )
        with queue.host_lock("finlab", root=root):
            provider_process.supervised_call("finlab.get", {"token": "offline", "dataset": "x"})


def test_parent_kill_keeps_worker_lock_until_worker_exits(tmp_path):
    started = tmp_path / "started"
    context = multiprocessing.get_context("spawn")
    parent = context.Process(target=_killed_parent, args=(tmp_path, tmp_path / "sdk", str(started)))
    parent.start()
    deadline = time.monotonic() + 10
    while not started.exists():
        assert time.monotonic() < deadline, "worker did not start"
        time.sleep(0.01)
    parent.kill()
    parent.join(10)
    with pytest.raises(queue.ProviderWorkWaitingError):
        with queue.host_lock("finlab", root=tmp_path):
            pass
    deadline = time.monotonic() + 10
    while True:
        try:
            with queue.host_lock("finlab", root=tmp_path):
                break
        except queue.ProviderWorkWaitingError:
            assert time.monotonic() < deadline, "orphaned worker never released its lock"
            time.sleep(0.05)
