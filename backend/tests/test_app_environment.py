"""APP_ENVIRONMENT is the only environment identity; the legacy bridge is retired."""

import base64
import hashlib
import os
import re
import subprocess

import pytest

from app.config import Settings
from tests.test_deployment_checks import REPO_ROOT, _load_workflow


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_settings_use_only_the_canonical_environment(monkeypatch, environment):
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    monkeypatch.delenv("DEPLOYMENT_TARGET", raising=False)
    assert Settings(_env_file=None).APP_ENVIRONMENT == environment
    assert not hasattr(Settings(_env_file=None), "DEPLOYMENT_TARGET")
    # The retired metadata is ignored whether or not it agrees.
    monkeypatch.setenv("DEPLOYMENT_TARGET", environment)
    assert Settings(_env_file=None).APP_ENVIRONMENT == environment
    monkeypatch.setenv(
        "DEPLOYMENT_TARGET", "production" if environment != "production" else "staging"
    )
    assert Settings(_env_file=None).APP_ENVIRONMENT == environment


def test_legacy_metadata_never_supplies_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_ENVIRONMENT", raising=False)
    monkeypatch.setenv("DEPLOYMENT_TARGET", "production")
    assert Settings(_env_file=None).APP_ENVIRONMENT == "local"
    monkeypatch.delenv("DEPLOYMENT_TARGET")
    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_ENVIRONMENT=staging\nDEPLOYMENT_TARGET=production\n")
    assert Settings(_env_file=dotenv).APP_ENVIRONMENT == "staging"
    assert Settings(_env_file=None).APP_ENVIRONMENT == "local"


def test_historical_backfill_window_is_configuration(monkeypatch):
    monkeypatch.delenv("HISTORICAL_BACKFILL_WINDOW_DAYS", raising=False)
    assert Settings(_env_file=None).HISTORICAL_BACKFILL_WINDOW_DAYS == 31
    monkeypatch.setenv("HISTORICAL_BACKFILL_WINDOW_DAYS", "366")
    assert Settings(_env_file=None).HISTORICAL_BACKFILL_WINDOW_DAYS == 366
    monkeypatch.setenv("HISTORICAL_BACKFILL_WINDOW_DAYS", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


@pytest.mark.parametrize("unit", ["findb", "fetcher"])
@pytest.mark.parametrize(
    "mode,marker,expected",
    [
        ("candidate", True, True),
        ("replay", True, True),
        ("candidate", False, False),
        ("replay", False, False),
    ],
)
def test_workflow_requires_the_environment_contract_marker_in_every_mode(
    tmp_path, unit, mode, marker, expected
):
    workflow = _load_workflow(REPO_ROOT / ".github/workflows" / (unit + "-deploy.yml"))
    script = next(
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "environment_contract_gate=" in step.get("run", "")
    )
    block = script[script.index("environment_contract_gate=") : script.index("host_command=")]
    line = next(line for line in script.splitlines() if line.startswith('host_command="'))
    # Generate the actual host command without executing its AWS/download prefix.
    names = set(re.findall(r"\$([A-Za-z_][A-Za-z_0-9]*)", line))
    env = {
        **os.environ,
        **{name: "fixture" for name in names},
        "MODE": mode,
        "q_target": "staging",
        "runtime_exports": "",
    }
    if unit == "fetcher":
        # Exercise the real gate's transport/checksum/subshell with a local guard;
        # a placeholder word here is not a valid command prefix before the gate.
        guard = b'set -euo pipefail\n[ "$APP_ENVIRONMENT" = staging ]\n'
        env["q_guard_b64"] = base64.b64encode(guard).decode("ascii")
        env["q_guard_sha"] = hashlib.sha256(guard).hexdigest()
        gate = next(
            line for line in script.splitlines() if line.startswith('checkpoint_entry_gate="')
        )
        block = gate + "\n" + block
    built = subprocess.run(
        ["/bin/bash", "-c", "set -euo pipefail\n" + block + line + '\nprintf "%s" "$host_command"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    start = built.index('; [ "${APP_ENVIRONMENT+x}"') + 2
    tail = built[start : built.index('; printf "' + unit + "_deploy_marker=")]
    assert "materialize-bundle --bundle" in built[:start]
    assert "--expected-bundle-sha256" in built[:start]
    assert "DEPLOYMENT_TARGET" not in built
    root = tmp_path / "release"
    helper = root / "infra/deploy/runtime-secrets" / ("deploy_" + unit + "_aws.sh")
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "#!/bin/bash\n"
        + ("# findb_environment_contract=app-environment-v1\n" if marker else "")
        + 'set -euo pipefail\nprintf "%s:%s\\n" "$APP_ENVIRONMENT" "${DEPLOYMENT_TARGET-}"\n'
    )
    helper.chmod(0o755)
    env = {**os.environ}
    env.pop("APP_ENVIRONMENT", None)
    env.pop("DEPLOYMENT_TARGET", None)
    result = subprocess.run(
        ["/bin/bash", "-c", "set -euo pipefail\nroot=" + str(root) + "\n" + tail],
        env=env,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is expected, result.stderr
    if expected:
        assert result.stdout.strip() == "staging:"


def test_provider_release_never_emits_legacy_environment_metadata():
    source = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    block = source[
        source.index("runtime_env_args=(\n") : source.index('\nsudo mkdir -p "$state_dir"')
    ]
    assert "DEPLOYMENT_TARGET" not in source
    assert "FETCHER_LEGACY_ENV_BRIDGE" not in source
    assert "FETCHER_ACCEPTED_REPLAY" not in source
    shell = (
        "set -euo pipefail\nprovider=finlab\nprovider_env=(--env FINLAB_API_TOKEN)\n"
        + block
        + '\nprintf "%s\\n" "${runtime_env_args[@]}"'
    )
    result = subprocess.run(
        ["/bin/bash", "-c", shell],
        env={
            **os.environ,
            "APP_ENVIRONMENT": "production",
            "FETCHER_CONSUMER_PROFILE": "full_market",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert "APP_ENVIRONMENT" in args
    assert "FINLAB_API_TOKEN" in args
    assert not any(arg.startswith("DEPLOYMENT_TARGET") for arg in args)
