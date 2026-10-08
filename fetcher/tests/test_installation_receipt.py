"""Local evidence inspects the running process and hashes all installed source."""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from findb_fetcher import full_market_cli, installation_receipt


def test_local_receipt_binds_actual_process_env_and_package(tmp_path, monkeypatch):
    config = tmp_path / "full.json"
    value = json.loads(
        (Path(__file__).parents[1] / "configs/full_market.local.v1.json").read_bytes()
    )
    config.write_text(json.dumps(value))
    governor = tmp_path / "governor.sqlite3"
    governor.touch()
    allocation = tmp_path / "finlab.account.json"
    allocation.write_text(
        json.dumps(
            {
                "provider": "finlab",
                "environment": "local",
                "requests_per_second": 3,
                "account": {
                    "governor_identity": str(governor),
                    "consumers": ["full_market", "maintenance"],
                },
            }
        )
    )
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "receipt",
            "--pid",
            "17",
            "--config",
            str(config),
            "--governor",
            str(governor),
            "--allocation",
            str(allocation),
            "--output",
            str(output),
        ],
    )
    process = f"findb-fetch-full-market --config {config}"
    env = f"{process} APP_ENVIRONMENT=local FULL_MARKET_ENABLED=true SOURCE_CLIENT_KEY=test-source-private FETCHER_ACCOUNT_STATE_PATH={governor} FETCHER_ACCOUNT_ALLOCATION_FILE={allocation}"

    def inspect(command, **kwargs):
        if command[-1] == "lstart=":
            return datetime.now().strftime("%a %b %d %H:%M:%S %Y")
        return env if command[1] == "eww" else process

    monkeypatch.setattr(installation_receipt.subprocess, "check_output", inspect)
    installation_receipt.main()
    receipt = json.loads(output.read_bytes())
    artifact = installation_receipt.installed_artifact_digest(Path(full_market_cli.__file__).parent)
    assert receipt["artifact_sha256"] == artifact
    assert receipt["source_key_sha256"] == hashlib.sha256(b"test-source-private").hexdigest()
    assert "test-source-private" not in output.read_text()
    installation_receipt.main()
    assert json.loads(output.read_bytes())["runtime_id"] == receipt["runtime_id"]
    env = env.replace("FULL_MARKET_ENABLED=true", "FULL_MARKET_ENABLED=false")
    with pytest.raises(ValueError, match="environment/flag"):
        installation_receipt.main()
    env = env.replace("FULL_MARKET_ENABLED=false", "FULL_MARKET_ENABLED=true").replace(
        str(allocation), "/other/allocation"
    )
    with pytest.raises(ValueError, match="allocation path"):
        installation_receipt.main()


def test_local_artifact_identity_changes_with_provider_source(tmp_path):
    (tmp_path / "runtime.py").write_text("runtime=1")
    before = installation_receipt.installed_artifact_digest(tmp_path)
    (tmp_path / "provider.py").write_text("provider=2")
    assert installation_receipt.installed_artifact_digest(tmp_path) != before


def test_legacy_process_inspection_is_explicit_and_rejects_conflicts():
    inspect = installation_receipt.process_environment
    assert inspect("APP_ENVIRONMENT=local") == ("local", "app-environment-v1")
    assert inspect("APP_ENVIRONMENT=local DEPLOYMENT_TARGET=local") == (
        "local",
        "app-environment-v1",
    )
    with pytest.raises(ValueError, match="conflict"):
        inspect("APP_ENVIRONMENT=local DEPLOYMENT_TARGET=production", allow_legacy=True)
    with pytest.raises(ValueError, match="explicit legacy"):
        inspect("DEPLOYMENT_TARGET=local")
    assert inspect("DEPLOYMENT_TARGET=local", allow_legacy=True) == (
        "local",
        "legacy-deployment-target-v1",
    )
