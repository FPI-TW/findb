"""Post-FinDB rollout recovery gate tests."""

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from script_env import script_env

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

    assert len(parsed) == 12
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
    deployment = names.index("Run candidate acceptance or accepted replay through bounded SSM")
    assert wait < deployment

    checkout = next(
        step for step in steps if step.get("name") == "Load deployment driver from protected main"
    )
    assert checkout["with"]["ref"] == "${{ github.workflow_sha }}"
    run = steps[deployment]["run"]
    assert ".deployment-driver/infra/deploy/check_fetcher_runtime_recovery.py" in run
    assert "--stable-seconds 0 --timeout-seconds 180 --poll-seconds 5" in run
    assert 'host_command="$(printf' in run


@pytest.mark.parametrize("case", ["healthy", "unhealthy", "tampered", "not_required"])
def test_embedded_recovery_must_succeed_before_any_apply_command(tmp_path, case):
    from test_full_market_checkpoint_entry import _transport

    _, binaries = _transport(tmp_path)
    steps = yaml.load(
        (ROOT / ".github/workflows/fetcher-deploy.yml").read_text(), Loader=yaml.BaseLoader
    )["jobs"]["deploy"]["steps"]
    run = next(step["run"] for step in steps if step.get("id") == "ssm")
    setup = run[run.index('recovery_gate=""') : run.index("runtime_variable_names=")]
    guard = tmp_path / ".deployment-driver/infra/deploy/check_fetcher_runtime_recovery.py"
    log = tmp_path / "order"
    wrapper = guard.with_name("ssm_bash_command.py")
    wrapper.write_bytes((ROOT / "infra/deploy/ssm_bash_command.py").read_bytes())
    timeout = Path(binaries) / "timeout"
    timeout.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        '[ "$1" = --signal=TERM ]\n[ "$2" = --kill-after=180s ]\n'
        'printf "%s\\n" "$3" >> "$TIMEOUT_LOG"\nshift 3\nexec "$@"\n'
    )
    timeout.chmod(0o700)
    wrap_start = run.index('host_command="$(printf')
    wrap = run[wrap_start : run.index("command_id=", wrap_start)]
    apply = "printf 'apply\\n' >> " + shlex.quote(str(log))
    setup += "\nhost_command=" + shlex.quote(apply) + "\n" + wrap
    guard.write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['ORDER_LOG']).write_text('recovery\\n')\n"
        "pathlib.Path(os.environ['GUARD_PATH']).write_text(__file__)\n"
        "sys.exit(2 if os.environ['RECOVERY_CASE'] == 'unhealthy' else 0)\n"
    )
    env = {
        **script_env(),
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "FINDB_ROLLOUT_SHA": "" if case == "not_required" else "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "RECOVERY_CASE": case,
        "ORDER_LOG": str(log),
        "TIMEOUT_LOG": str(tmp_path / "timeouts"),
        "GUARD_PATH": str(tmp_path / "guard-path"),
    }
    built = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + setup + '\nprintf "%s" "$host_command"'],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    prefix = built.stdout
    if case == "tampered":
        import hashlib

        prefix = prefix.replace(hashlib.sha256(guard.read_bytes()).hexdigest(), "0" * 64)
    result = subprocess.run(
        ["bash", "-c", prefix],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert (result.returncode == 0) == (case in ("healthy", "not_required")), result.stderr
    expected = {
        "healthy": "recovery\napply\n",
        "unhealthy": "recovery\n",
        "tampered": "",
        "not_required": "apply\n",
    }[case]
    assert (log.read_text() if log.exists() else "") == expected
    bounds = (
        ["900s"] if case == "not_required" else ["240s"] + (["900s"] if case == "healthy" else [])
    )
    assert (tmp_path / "timeouts").read_text().splitlines() == bounds
    if (tmp_path / "guard-path").exists():
        assert not Path((tmp_path / "guard-path").read_text()).exists()
    assert run.count("aws ssm send-command") == 1
    assert '"set -euo pipefail; ($recovery_command); $host_command"' in run
    assert "--timeout-seconds 240" in run and "--timeout-seconds 900" in run
