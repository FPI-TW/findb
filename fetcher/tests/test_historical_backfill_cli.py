"""Real process signals must drain a leased date without claiming another."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading

import pytest

from findb_fetcher.historical_backfill_cli import run_worker

PROCESS = r"""
import sys, time
from uuid import UUID
from findb_fetcher import historical_backfill_cli as cli

mode = sys.argv[1]
class Control:
    def __enter__(self): return self
    def __exit__(self, *args): print("closed", flush=True)
    def claim(self):
        print("claim", flush=True)
        return None if mode == "idle" else object()
    def complete(self, item, run_id): print("completed", flush=True)
    def fail(self, item, code): print("failed", flush=True)
class Runner:
    @classmethod
    def from_env(cls):
        if mode == "startup":
            print("startup", flush=True)
            time.sleep(0.5)
        return cls()
    def run(self, item):
        print("running", flush=True)
        time.sleep(0.5)
        if mode == "failure": raise ValueError("secret-provider-error")
        return UUID("01982886-b452-7c71-b166-cb74cfd1d002")
cli.TwelveDataHistoricalRunner = Runner
cli.FetcherConfig.from_env = lambda: object()
cli.HistoricalControlClient = lambda config: Control()
raise SystemExit(cli.main(["--provider", "twelve_data", "--run-forever"]))
"""


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("mode", ["idle", "running", "failure", "startup"])
def test_process_signal_drains_and_exits(mode: str, signum: signal.Signals) -> None:
    with subprocess.Popen(
        [sys.executable, "-u", "-c", PROCESS, mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        expected = "claim" if mode == "idle" else "startup" if mode == "startup" else "running"
        lines: list[str] = []
        try:
            captured = b""
            while expected not in captured.decode().splitlines():
                ready, _, _ = select.select([process.stdout], [], [], 5)
                assert ready, "worker did not reach signal boundary"
                captured += os.read(process.stdout.fileno(), 4096)
            lines.extend(captured.decode().splitlines())
            process.send_signal(signum)
            stdout, stderr = process.communicate(timeout=3)
            lines.extend(stdout.splitlines())
            assert process.returncode == 0, stderr
            assert lines.count("claim") == (0 if mode == "startup" else 1)
            assert lines[-1] == "closed"
            if mode in {"running", "failure"}:
                assert ("failed" if mode == "failure" else "completed") in lines
            assert "secret-provider-error" not in stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_already_stopped_worker_never_claims() -> None:
    stopped = threading.Event()
    stopped.set()
    # No control/runner methods can be called when the stop is already set.
    run_worker(None, None, run_forever=True, stop_event=stopped)  # type: ignore[arg-type]


def test_one_shot_drains_until_no_work(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = iter((True, True, False))
    calls: list[bool] = []

    def run_once(*_: object) -> bool:
        result = next(outcomes)
        calls.append(result)
        return result

    monkeypatch.setattr("findb_fetcher.historical_backfill_cli.run_once", run_once)
    run_worker(None, None, run_forever=False, stop_event=threading.Event())  # type: ignore[arg-type]
    assert calls == [True, True, False]
