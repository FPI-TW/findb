"""Signal errors never substitute for process/group death evidence."""

import multiprocessing
import os
import signal
import time

import pytest

from findb_fetcher.provider_process import supervised_call
from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError


@pytest.mark.parametrize("signal_error", [signal.SIGTERM, signal.SIGKILL])
def test_signal_race_still_reaps_exited_child_and_verifies_group(monkeypatch, signal_error):
    killpg = os.killpg
    observed = []

    def raced(pid, sig):
        observed.append(sig)
        if sig == signal_error:
            raise PermissionError("simulated exiting-group race")
        return killpg(pid, sig)

    before = {p.pid for p in multiprocessing.active_children()}
    monkeypatch.setattr(os, "killpg", raced)
    assert supervised_call(lambda: "completed") == "completed"
    assert signal_error in observed and 0 in observed
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_denied_signals_cannot_release_an_unreaped_live_child(monkeypatch):
    killpg = os.killpg
    before = {p.pid for p in multiprocessing.active_children()}

    def denied(pid, sig):
        if sig:
            raise PermissionError("simulated signal denial")
        return killpg(pid, sig)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "killpg", denied)
            with pytest.raises(ProviderWorkRecoveryBlockedError, match="not been reaped"):
                supervised_call(lambda: time.sleep(30), timeout=0.05)
    finally:
        for child in multiprocessing.active_children():
            if child.pid not in before:
                killpg(child.pid, signal.SIGKILL)
                child.join(2)
                assert not child.is_alive()
                child.close()


def test_unverifiable_group_is_recovery_blocked_even_when_leader_reaped(monkeypatch):
    killpg = os.killpg

    def cannot_verify(pid, sig):
        if sig == 0:
            raise PermissionError("simulated group probe denial")
        return killpg(pid, sig)

    monkeypatch.setattr(os, "killpg", cannot_verify)
    with pytest.raises(ProviderWorkRecoveryBlockedError, match="cannot be verified"):
        supervised_call(lambda: "completed")
