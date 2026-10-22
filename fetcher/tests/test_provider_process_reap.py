"""Signal errors never substitute for process/group death evidence."""

import os
import signal

import pytest

from findb_fetcher.provider_process import supervised_call
from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError
from provider_worker_support import fake_finlab, install_fake_sdk

SLEEPING_GET = """
if dataset == "sleep":
    time.sleep(30)
return "completed"
"""


@pytest.fixture
def launched(monkeypatch, tmp_path):
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", fake_finlab(SLEEPING_GET))


def _call(dataset, **kwargs):
    return supervised_call(
        "finlab.get",
        {"token": "offline", "dataset": dataset, "symbols": ("2330",), "account": False},
        **kwargs,
    )


@pytest.mark.parametrize("signal_error", [signal.SIGTERM, signal.SIGKILL])
def test_signal_race_still_reaps_exited_child_and_verifies_group(
    monkeypatch, launched, signal_error
):
    killpg = os.killpg
    observed = []

    def raced(pid, sig):
        observed.append(sig)
        if sig == signal_error:
            raise PermissionError("simulated exiting-group race")
        return killpg(pid, sig)

    monkeypatch.setattr(os, "killpg", raced)
    assert _call("done") == ("completed", None)
    assert signal_error in observed and 0 in observed
    assert [process.poll() is not None for process in launched] == [True]


def test_denied_signals_cannot_release_an_unreaped_live_child(monkeypatch, launched):
    killpg = os.killpg

    def denied(pid, sig):
        if sig:
            raise PermissionError("simulated signal denial")
        return killpg(pid, sig)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "killpg", denied)
            with pytest.raises(ProviderWorkRecoveryBlockedError, match="not been reaped"):
                _call("sleep", timeout=0.05)
    finally:
        for process in launched:
            if process.poll() is None:
                killpg(process.pid, signal.SIGKILL)
                process.wait(2)
            assert process.poll() is not None


def test_unverifiable_group_is_recovery_blocked_even_when_leader_reaped(monkeypatch, launched):
    killpg = os.killpg

    def cannot_verify(pid, sig):
        if sig == 0:
            raise PermissionError("simulated group probe denial")
        return killpg(pid, sig)

    monkeypatch.setattr(os, "killpg", cannot_verify)
    with pytest.raises(ProviderWorkRecoveryBlockedError, match="cannot be verified"):
        _call("done")
