"""Cutover refuses mixed hosts/protocols/mounts and actual held SDK locks."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "provider_work_preflight", ROOT / "infra/deploy/provider_work_preflight.py"
)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def inventory(root):
    for provider in preflight.PROVIDERS:
        (root / f"provider-work-staging-{provider}.lock").touch(mode=0o600)
    return [
        {
            "Name": f"/findb-{profile.replace('_', '-')}-{provider.replace('_', '-')}",
            "State": {"Running": False, "ExitCode": 0, "OOMKilled": False, "Error": ""},
            "Config": {
                "Env": [
                    "APP_ENVIRONMENT=staging",
                    "PROVIDER_WORK_REQUIRED=true",
                    "FETCHER_ACCOUNT_STATE_PATH=/var/lib/findb-account/staging/governor.sqlite3",
                ],
                "Labels": {"com.findb.provider-work.protocol": "v1"},
            },
            "Mounts": [
                {
                    "Destination": "/var/lib/findb-account/staging",
                    "Type": "bind",
                    "RW": True,
                    "Source": str(root),
                }
            ],
        }
        for profile in ("pilot", "full_market", "historical")
        for provider in preflight.PROVIDERS
    ]


def test_drained_single_host_shared_inodes_emit_no_secrets(tmp_path):
    rows = inventory(tmp_path)
    proof = preflight.inspect_cutover(
        environment="staging", host_id="one", host_ids=["one"], containers=rows
    )
    assert set(proof["host_lock_identities"]) == set(preflight.PROVIDERS)
    assert len(proof["inventory_sha256"]) == 64
    assert "Env" not in json.dumps(proof)


@pytest.mark.parametrize(
    "fault", ["second-host", "old-protocol", "wrong-mount", "active-consumer", "missing-consumer"]
)
def test_cutover_rejects_ambiguous_external_io_ownership(tmp_path, fault):
    rows = copy.deepcopy(inventory(tmp_path))
    hosts = ["one"]
    if fault == "second-host":
        hosts.append("two")
    if fault == "old-protocol":
        rows[0]["Config"]["Labels"] = {}
    if fault == "wrong-mount":
        rows[0]["Mounts"][0]["Source"] += "-other"
    if fault == "active-consumer":
        rows[0]["State"]["Running"] = True
    if fault == "missing-consumer":
        rows.pop()
    with pytest.raises(ValueError):
        preflight.inspect_cutover(
            environment="staging", host_id="one", host_ids=hosts, containers=rows
        )


@pytest.mark.parametrize("provider", preflight.PROVIDERS)
def test_inode_bootstrap_does_not_make_a_held_work_lock_attestable(tmp_path, monkeypatch, provider):
    from findb_fetcher import provider_work

    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    rows = inventory(tmp_path)
    with provider_work.host_lock(provider, root=tmp_path):
        provider_work.initialize_host_lock(provider, root=tmp_path)
        with pytest.raises(BlockingIOError):
            preflight.inspect_cutover(
                environment="staging", host_id="one", host_ids=["one"], containers=rows
            )
        with pytest.raises(provider_work.ProviderWorkWaitingError):
            with provider_work.host_lock(provider, root=tmp_path):
                pytest.fail("preflight or initialization released an active work lock")
    proof = preflight.inspect_cutover(
        environment="staging", host_id="one", host_ids=["one"], containers=rows
    )
    assert set(proof["host_lock_identities"]) == set(preflight.PROVIDERS)
