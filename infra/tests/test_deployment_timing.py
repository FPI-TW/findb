"""Exercise deployment deadlines and safe remote failure diagnostics offline."""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "infra/deploy/ssm_command_marker_gate.sh"


def run_wait(tmp_path, *, status, stdout="", seconds=5, response_delay=0, confirm=True):
    response = {
        "command_id": "command",
        "instance_id": "instance",
        "document_name": "AWS-RunShellScript",
        "plugin_name": "aws:runShellScript",
        "status": status,
        "status_details": status,
        "response_code": 0 if status == "Success" else 2,
        "stdout": stdout,
    }
    aws = tmp_path / "aws"
    aws.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "args=sys.argv[1:]\n"
        "root=Path(os.environ['FAKE_ROOT'])\n"
        "with (root/'calls').open('a') as f: f.write(json.dumps(args)+'\\n')\n"
        "if args[:2] == ['ssm','cancel-command']:\n"
        "    (root/'cancelled').touch()\n"
        "elif args[args.index('--query')+1] == 'Status':\n"
        "    print(('Cancelled' if os.environ['FAKE_CONFIRM'] == 'true' else 'Unknown') if (root/'cancelled').exists() else 'InProgress')\n"
        "else:\n"
        "    time.sleep(float(os.environ['FAKE_DELAY']))\n"
        "    print(os.environ['FAKE_RESPONSE'])\n"
    )
    aws.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\n/bin/sleep 0.05\n")
    sleep.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{GATE}"; ssm_wait_command region command instance '
            f'"marker status=success" "marker status=failed" {seconds}',
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "FAKE_ROOT": str(tmp_path),
            "FAKE_RESPONSE": json.dumps(response),
            "GITHUB_ACTIONS": "true",
            "FAKE_DELAY": str(response_delay),
            "FAKE_CONFIRM": str(confirm).lower(),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result, [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]


def test_success_requires_marker_without_cancelling_remote_command(tmp_path):
    result, calls = run_wait(tmp_path, status="Success", stdout="marker status=success\n")
    assert result.returncode == 0, result.stderr
    assert not any("cancel-command" in call for call in calls)


def test_deadline_cancels_and_confirms_remote_termination_without_acceptance(tmp_path):
    result, calls = run_wait(tmp_path, status="InProgress", seconds=1)
    assert result.returncode != 0
    assert "deadline exceeded" in result.stderr
    assert "status=Cancelled" in result.stderr
    cancel = next(call for call in calls if "cancel-command" in call)
    assert cancel[cancel.index("--command-id") + 1] == "command"
    assert cancel[cancel.index("--instance-ids") + 1] == "instance"
    assert result.stdout == ""


def test_success_received_after_deadline_cannot_be_accepted(tmp_path):
    result, calls = run_wait(
        tmp_path,
        status="Success",
        stdout="marker status=success\n",
        seconds=1,
        response_delay=1.1,
    )
    assert result.returncode != 0
    assert "deadline exceeded" in result.stderr
    assert any("cancel-command" in call for call in calls)


def test_unconfirmed_remote_termination_reports_command_identity(tmp_path):
    result, _ = run_wait(tmp_path, status="Failed", confirm=False)
    assert result.returncode != 0
    assert "SSM remote termination unconfirmed: command_id=command" in result.stderr
    assert "acceptance is blocked" in result.stderr


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
def test_scheduler_block_surfaces_only_safe_profile_and_provider(tmp_path, profile, provider):
    diagnostic = (
        "fetcher_deployment_blocked reason=scheduler_not_stopped "
        f"execution_profile={profile} provider={provider}"
    )
    result, _ = run_wait(
        tmp_path,
        status="Failed",
        stdout=f"secret-token-must-not-leak\n{diagnostic}\n{diagnostic} secret\n",
    )
    assert result.returncode != 0
    assert f"scheduler_not_stopped execution_profile={profile} provider={provider}" in result.stderr
    assert "Stop the scheduler in Dashboard" in result.stderr
    assert "::error title=Fetcher scheduler 尚未停止::" in result.stderr
    assert "secret" not in result.stderr
    assert result.stderr.count("Fetcher deployment blocked:") == 1


@pytest.mark.parametrize("profile", ["pilot", "full_market", "historical"])
@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex", "factset"])
def test_host_preflight_block_surfaces_identity_without_raw_output(tmp_path, profile, provider):
    diagnostic = (
        "fetcher_host_blocked reason=runtime_not_eligible "
        f"execution_profile={profile} provider={provider}"
    )
    result, _ = run_wait(
        tmp_path,
        status="Failed",
        stdout=f"secret-token\n{diagnostic}\n{diagnostic}\n{diagnostic} secret\n",
    )
    assert result.returncode != 0
    assert f"runtime_not_eligible execution_profile={profile} provider={provider}" in result.stderr
    assert "safely recover its original owner" in result.stderr
    assert "::error title=Fetcher 主機 runtime 未通過檢查::" in result.stderr
    assert "secret" not in result.stderr
    assert result.stderr.count("Fetcher host preflight blocked:") == 1


@pytest.mark.parametrize(
    "identity",
    [
        "execution_profile=unknown provider=twelve_data",
        "execution_profile=pilot provider=unknown",
        "execution_profile=pilot provider=twelve_data%0Asecret",
    ],
)
def test_host_preflight_block_rejects_untrusted_identity(tmp_path, identity):
    result, _ = run_wait(
        tmp_path,
        status="Failed",
        stdout=f"fetcher_host_blocked reason=runtime_not_eligible {identity}\n",
    )
    assert result.returncode != 0
    assert "Fetcher host preflight blocked:" not in result.stderr
    assert "::error title=Fetcher 主機" not in result.stderr
    assert "secret" not in result.stderr


def test_host_command_has_a_deadline_and_time_for_term_recovery():
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "infra/deploy/ssm_bash_command.py"),
            "--timeout-seconds",
            "900",
        ],
        input="trap 'exit 143' TERM\nprintf 'safe'\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.startswith("exec timeout --signal=TERM --kill-after=180s 900s bash -c ")
