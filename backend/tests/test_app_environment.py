"""Canonical environment identity and narrow immutable replay bridges."""

import subprocess

import pytest
from pydantic import ValidationError

from app.config import Settings
from tests.script_env import script_env
from tests.test_deployment_checks import REPO_ROOT


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


@pytest.mark.parametrize("consumer", ["pilot", "full_market", "historical"])
def test_current_runtime_only_injects_canonical_environment(consumer):
    source = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    block = source[
        source.index("runtime_env_args=(\n") : source.index('\nsudo mkdir -p "$state_dir"')
    ]
    shell = (
        "set -euo pipefail\nprovider=finlab\nprovider_env=(--env FINLAB_API_TOKEN)\n"
        + block
        + '\nprintf "%s\\n" "${runtime_env_args[@]}"'
    )
    result = subprocess.run(
        ["/bin/bash", "-c", shell],
        env={**script_env(), "APP_ENVIRONMENT": "production", "FETCHER_CONSUMER_PROFILE": consumer},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "APP_ENVIRONMENT" in result.stdout.splitlines()
    assert "DEPLOYMENT_TARGET" not in result.stdout
