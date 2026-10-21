"""A timed-out or OOM-killed writer must block schema migration."""

import os
import subprocess
from pathlib import Path

import pytest
from script_env import script_env

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("exit_code", [0, 1, 137, 143])
@pytest.mark.parametrize("oom", ["true", "false"])
def test_actual_writer_stop_script_checks_terminal_exit(tmp_path, exit_code, oom):
    deployment = (ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text()
    script = deployment.split("<<'STOP_SCRIPT'\n", 1)[1].split("\nSTOP_SCRIPT", 1)[0]
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"ExitCode"*) printf "%s %s\\n" "$TEST_EXIT" "$TEST_OOM";;\n'
        '  *"State.Status"*) echo "exited false false";;\n'
        '  *"ps -aq"*) echo fixture-container;;\n'
        '  *"stop --timeout 30"*) exit 0;;\n'
        "  *) exit 91;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", "-s", "--", "compose.yml", "worker"],
        input=script,
        text=True,
        capture_output=True,
        env={
            **script_env(),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "TEST_EXIT": str(exit_code),
            "TEST_OOM": oom,
        },
    )
    assert (result.returncode == 0) == (exit_code in {0, 143} and oom == "false")
    if result.returncode:
        assert "writer_shutdown_not_graceful" in result.stderr
