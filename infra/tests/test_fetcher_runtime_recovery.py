"""Post-FinDB rollout recovery gate tests."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra/deploy/check_fetcher_runtime_recovery.py"


def load_module():
    spec = importlib.util.spec_from_file_location("fetcher_runtime_recovery", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recovery = load_module()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def snapshot(*, generation="original", healthy=True):
    return tuple(
        (
            name,
            f"{generation}-{index}",
            f"image-{index}",
            3 if generation == "original" else 4,
            f"started-{generation}",
            healthy,
        )
        for index, name in enumerate(recovery.EXPECTED_RUNTIMES)
    )


def docker_rows(*, unhealthy_name=None):
    rows = []
    for index, name in enumerate(recovery.EXPECTED_RUNTIMES):
        healthy = name != unhealthy_name
        rows.append(
            {
                "Id": f"container-{index}",
                "Name": f"/{name}",
                "RestartCount": index,
                "Config": {
                    "Image": f"image-{index}",
                    "Labels": {"com.findb.fetcher.accepted": "true"},
                },
                "State": {
                    "Running": healthy,
                    "Status": "running" if healthy else "restarting",
                    "ExitCode": 0 if healthy else 2,
                    "OOMKilled": False,
                    "Error": "",
                    "StartedAt": f"started-{index}",
                },
            }
        )
    return rows


def test_stable_runtime_set_passes_only_after_complete_window():
    clock = FakeClock()
    calls = 0

    def read():
        nonlocal calls
        calls += 1
        return snapshot()

    result = recovery.wait_until_recovered(
        read,
        stable_seconds=10,
        timeout_seconds=30,
        poll_seconds=5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result == snapshot()
    assert calls == 3
    assert clock.now == 10


def test_restart_resets_the_stability_window():
    clock = FakeClock()
    observations = iter([snapshot(), snapshot(generation="restarted")])
    latest = snapshot(generation="restarted")

    def read():
        return next(observations, latest)

    result = recovery.wait_until_recovered(
        read,
        stable_seconds=10,
        timeout_seconds=30,
        poll_seconds=5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result == latest
    assert clock.now == 15


def test_unhealthy_runtime_fails_closed_at_bounded_deadline():
    clock = FakeClock()

    with pytest.raises(
        recovery.RecoveryTimeoutError,
        match="^runtime_not_recovered_after_findb_rollout$",
    ):
        recovery.wait_until_recovered(
            lambda: snapshot(healthy=False),
            stable_seconds=10,
            timeout_seconds=12,
            poll_seconds=5,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

    assert clock.now == 12


def test_empty_first_bootstrap_does_not_wait_for_installed_runtimes():
    clock = FakeClock()

    result = recovery.wait_until_recovered(
        lambda: (),
        stable_seconds=60,
        timeout_seconds=180,
        poll_seconds=5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result == ()
    assert clock.now == 0


def test_runtime_parser_requires_exact_accepted_inventory():
    parsed = recovery._parse_runtime_rows(docker_rows())

    assert len(parsed) == 15
    assert all(row[-1] for row in parsed)
    with pytest.raises(ValueError, match="inventory is incomplete"):
        recovery._parse_runtime_rows(docker_rows()[:-1])
    unhealthy = recovery._parse_runtime_rows(docker_rows(unhealthy_name="findb-pilot-twelve-data"))
    assert not next(row for row in unhealthy if row[0] == "findb-pilot-twelve-data")[-1]


def test_real_cli_surfaces_only_fixed_failure_reason(tmp_path):
    docker = tmp_path / "docker"
    rows = docker_rows(unhealthy_name="findb-pilot-twelve-data")
    names = " ".join(f"'{name}'" for name in recovery.EXPECTED_RUNTIMES)
    docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "container ls" ]; then\n'
        f"  printf '%s\\n' {names}\n"
        'elif [ "$1" = inspect ]; then\n'
        f"  printf '%s\\n' '{json.dumps(rows)}'\n"
        "else exit 91; fi\n"
    )
    docker.chmod(0o700)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--stable-seconds",
            "0.01",
            "--timeout-seconds",
            "0.02",
            "--poll-seconds",
            "0.01",
        ],
        env={"PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert result.returncode == 1
    assert result.stdout == (
        "fetcher_aws_deploy=failed reason=runtime_not_recovered_after_findb_rollout\n"
    )
    assert result.stderr == ""


def test_workflow_runs_trusted_recovery_gate_after_findb_wait_before_mutation():
    workflow = yaml.load(
        (ROOT / ".github/workflows/fetcher-deploy.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]

    wait = names.index("Wait for same-revision FinDB staging rollout")
    recovery_gate = names.index("Wait for installed Fetcher runtimes to recover")
    deployment = names.index("Run candidate acceptance or accepted replay through bounded SSM")
    assert wait < recovery_gate < deployment

    checkout = next(
        step for step in steps if step.get("name") == "Checkout trusted checkpoint safety gate"
    )
    assert "infra/deploy/check_fetcher_runtime_recovery.py" in checkout["with"]["sparse-checkout"]
    run = steps[recovery_gate]["run"]
    assert ".trusted-checkpoint-safety/infra/deploy/check_fetcher_runtime_recovery.py" in run
    assert "--stable-seconds 60 --timeout-seconds 180 --poll-seconds 5" in run
    assert 'host_command="$(printf' in run
