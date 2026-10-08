"""Canonical environment identity and narrow immutable replay bridges."""

import base64
import hashlib
import os
import re
import subprocess

import pytest
from pydantic import ValidationError

from app.config import Settings
from tests.test_deployment_checks import REPO_ROOT, _load_workflow


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_settings_use_canonical_environment_and_same_value_legacy_metadata(
    monkeypatch, environment
):
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    monkeypatch.delenv("DEPLOYMENT_TARGET", raising=False)
    assert Settings(_env_file=None).APP_ENVIRONMENT == environment
    assert not hasattr(Settings(_env_file=None), "DEPLOYMENT_TARGET")
    monkeypatch.setenv("DEPLOYMENT_TARGET", environment)
    assert Settings(_env_file=None).APP_ENVIRONMENT == environment
    monkeypatch.setenv(
        "DEPLOYMENT_TARGET", "production" if environment != "production" else "staging"
    )
    with pytest.raises(ValidationError, match="conflicts"):
        Settings(_env_file=None)


def test_legacy_environment_cannot_supply_new_settings_input(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_ENVIRONMENT", raising=False)
    monkeypatch.setenv("DEPLOYMENT_TARGET", "local")
    with pytest.raises(ValidationError, match="conflicts"):
        Settings(_env_file=None)
    monkeypatch.delenv("DEPLOYMENT_TARGET")
    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_ENVIRONMENT=staging\nDEPLOYMENT_TARGET=production\n")
    with pytest.raises(ValidationError, match="conflicts"):
        Settings(_env_file=dotenv)
    assert Settings(_env_file=None).APP_ENVIRONMENT == "local"


@pytest.mark.parametrize("unit", ["findb", "fetcher"])
@pytest.mark.parametrize(
    "mode,old_helper,legacy,expected",
    [
        ("candidate", False, None, True),
        ("replay", False, None, True),
        ("replay", True, None, True),
        ("candidate", True, None, False),
        ("replay", True, "production", False),
    ],
)
def test_verified_workflow_bridge_executes_old_helpers_only_for_accepted_replay(
    tmp_path, unit, mode, old_helper, legacy, expected
):
    workflow = _load_workflow(REPO_ROOT / ".github/workflows" / (unit + "-deploy.yml"))
    script = next(
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "legacy_environment_bridge=" in step.get("run", "")
    )
    block = script[script.index("legacy_environment_bridge=") : script.index("host_command=")]
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
        # a placeholder word here is not a valid command prefix before the bridge.
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
    root = tmp_path / "release"
    helper = root / "infra/deploy/runtime-secrets" / ("deploy_" + unit + "_aws.sh")
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "#!/bin/bash\n"
        + ("" if old_helper else "# findb_environment_contract=app-environment-v1\n")
        + 'set -euo pipefail\nprintf "%s:%s\\n" "$APP_ENVIRONMENT" "${DEPLOYMENT_TARGET-}"\n'
    )
    helper.chmod(0o755)
    env = {**os.environ}
    env.pop("APP_ENVIRONMENT", None)
    env.pop("DEPLOYMENT_TARGET", None)
    if legacy is not None:
        env["DEPLOYMENT_TARGET"] = legacy
    result = subprocess.run(
        ["/bin/bash", "-c", "set -euo pipefail\nroot=" + str(root) + "\n" + tail],
        env=env,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is expected, result.stderr
    if expected:
        assert result.stdout.strip() == ("staging:staging" if old_helper else "staging:")


@pytest.mark.parametrize(
    "bridge,replay,consumer,mode,allowed",
    [
        ("false", "false", "pilot", "activate", True),
        ("true", "true", "full_market", "activate", True),
        ("true", "false", "full_market", "activate", False),
        ("true", "true", "pilot", "activate", False),
        ("true", "true", "full_market", "candidate", False),
    ],
)
def test_retained_old_full_image_gets_exact_same_environment_bridge(
    bridge, replay, consumer, mode, allowed
):
    source = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    block = source[
        source.index("runtime_env_args=(\n") : source.index('\nsudo mkdir -p "$state_dir"')
    ]
    shell = (
        "set -euo pipefail\nprovider=finlab\nprovider_env=(--env FINLAB_API_TOKEN)\n"
        + block
        + '\nprintf "%s\\n" "${runtime_env_args[@]}"'
    )
    env = {
        **os.environ,
        "APP_ENVIRONMENT": "production",
        "FETCHER_LEGACY_ENV_BRIDGE": bridge,
        "FETCHER_ACCEPTED_REPLAY": replay,
        "FETCHER_CONSUMER_PROFILE": consumer,
        "FETCHER_DEPLOY_MODE": mode,
    }
    result = subprocess.run(["/bin/bash", "-c", shell], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is allowed
    if allowed:
        args = result.stdout.splitlines()
        assert "APP_ENVIRONMENT" in args
        assert ("DEPLOYMENT_TARGET=production" in args) is (bridge == "true")
        assert "FINLAB_API_TOKEN" in args
