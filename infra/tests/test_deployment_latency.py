"""Exercise latency optimizations without relaxing publication or host gates."""

import importlib.util
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest
import yaml
from script_env import script_env

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "infra/deploy/runtime-secrets"


def loader():
    spec = importlib.util.spec_from_file_location(
        "latency_loader", RUNTIME / "load_runtime_secrets.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def catalog():
    return {
        "unit": "findb",
        "consumers": {
            "compose": {
                "secrets": [
                    {"name": f"secret/{i}", "required_keys": [f"KEY_{i}"]} for i in range(8)
                ]
            },
            "migration": {"secrets": [{"name": "migration", "required_keys": ["MIGRATION_URL"]}]},
        },
    }


def test_secret_reads_overlap_only_within_consumer_and_are_bounded():
    barrier = threading.Barrier(4, timeout=5)
    seen = []
    lock = threading.Lock()

    def fetch(name):
        with lock:
            seen.append(name)
        # Every group of four must overlap. Serial fetching deadlocks here.
        barrier.wait()
        i = name.rsplit("/", 1)[1]
        return json.dumps({f"KEY_{i}": f"value-{i}"})

    result = loader().load_consumer(catalog(), "compose", fetch)
    assert len(result) == 8
    assert set(seen) == {f"findb/staging/findb/secret/{i}" for i in range(8)}
    assert "MIGRATION_URL" not in result


def test_invalid_allowlist_fails_before_any_secret_request():
    data = catalog()
    data["consumers"]["compose"]["secrets"][-1]["name"] = "../migration"
    seen = []
    module = loader()
    with pytest.raises(module.LoaderError, match="catalog_invalid"):
        module.load_consumer(data, "compose", lambda name: seen.append(name))
    assert seen == []


def test_partial_secret_failure_never_produces_env(tmp_path, monkeypatch):
    module = loader()
    written = []
    monkeypatch.setattr(module, "write_env_file", lambda *a, **kw: written.append(a))
    monkeypatch.setattr(module, "_catalog", lambda path: catalog())

    def fetch(name):
        i = name.rsplit("/", 1)[1]
        if i == "3":
            raise module.LoaderError("secret_fetch_failed")
        return json.dumps({f"KEY_{i}": "sensitive-value"})

    monkeypatch.setattr(module, "_aws_fetcher", lambda region: fetch)
    monkeypatch.setattr(
        "sys.argv",
        [
            "loader",
            "--catalog",
            "catalog",
            "--consumer",
            "compose",
            "--region",
            "ap-southeast-1",
            "--deployment-target",
            "staging",
            "--output",
            str(tmp_path / "env"),
        ],
    )
    assert module.main() == 1
    assert written == []


@pytest.mark.parametrize("failure", ["", "pull", "digest"])
def test_parallel_pull_waits_for_all_and_rejects_partial_failure(tmp_path, failure):
    source = (RUNTIME / "deploy_fetcher_aws.sh").read_text()
    script = source.split("<<'PREPARE_IMAGES'\n", 1)[1].split("\nPREPARE_IMAGES", 1)[0]
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
root = Path(os.environ['PULL_TEST'])
a = sys.argv[1:]
if a[0] == 'pull':
    (root / (a[1] + '.started')).touch()
    deadline = time.monotonic() + 5
    while len(list(root.glob('*.started'))) != 3:
        if time.monotonic() > deadline: sys.exit(9)
        time.sleep(.01)
    (root / (a[1] + '.done')).touch()
    sys.exit(1 if a[1] == 'two' and os.environ['FAILURE'] == 'pull' else 0)
if a[:2] == ['image', 'inspect']:
    print('wrong' if a[2] == 'two' and os.environ['FAILURE'] == 'digest' else a[2])
    sys.exit(0)
sys.exit(99)
""")
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", "-s", "--", "one", "two", "three"],
        input=script,
        text=True,
        capture_output=True,
        timeout=15,
        env={
            **script_env(),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PULL_TEST": str(tmp_path),
            "FAILURE": failure,
        },
    )
    assert (result.returncode == 0) == (failure == ""), result.stderr
    assert len(list(tmp_path.glob("*.done"))) == 3


@pytest.mark.parametrize("unit", ["findb", "fetcher"])
@pytest.mark.parametrize(
    "target,mode,key,tag,ref,valid",
    [
        ("staging", "candidate", "", "", "refs/heads/main", True),
        ("production", "candidate", "", "findb-v1.2.3", "refs/heads/main", False),
        ("staging", "replay", "accepted", "", "refs/heads/main", False),
        ("staging", "candidate", "unexpected", "", "refs/heads/main", False),
        ("staging", "candidate", "", "", "refs/heads/feature", False),
    ],
)
def test_preparation_contract_rejects_cross_target_replay_and_unprotected_ref(
    unit,
    target,
    mode,
    key,
    tag,
    ref,
    valid,
):
    steps = yaml.safe_load((ROOT / f".github/workflows/{unit}-deploy.yml").read_text())["jobs"][
        "deploy"
    ]["steps"]
    guard = next(
        s for s in steps if s.get("name") == "Verify staging candidate preparation contract"
    )
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    result = subprocess.run(
        ["bash", "-c", guard["run"]],
        cwd=ROOT,
        env={
            **script_env(),
            "TARGET": target,
            "MODE": mode,
            "REVISION": head,
            "BUNDLE_KEY": key,
            "RELEASE_TAG": tag,
            "GITHUB_REF": ref,
        },
        capture_output=True,
    )
    assert (result.returncode == 0) == valid
    assert steps.index(guard) < next(i for i, s in enumerate(steps) if s.get("id") == "candidate")


@pytest.mark.parametrize("fault", ["", "missing", "duplicate", "exit_type", "command", "daemon"])
def test_initial_batch_inventory_validates_every_runtime_and_fails_closed(tmp_path, fault):
    source = (RUNTIME / "deploy_fetcher_aws.sh").read_text()
    role = source[source.index("runtime_command_role() {") : source.index("\nset +x")]
    inventory = source[source.index("initial_runtime_state() {") : source.index("\nprocessed=()")]
    rows = []
    for provider in ("twelve-data", "finlab", "taifex", "factset"):
        for profile in ("pilot", "full-market", "historical"):
            command = (
                ["findb-fetch-historical-backfill"]
                if profile == "historical"
                else ["findb-fetch-runtime", "--execution-profile", profile.replace("-", "_")]
            )
            rows.append(
                {
                    "name": f"/findb-{profile}-{provider}",
                    "id": f"id-{len(rows)}",
                    "image": "image@sha256:abc",
                    "running": True,
                    "exit_code": 0,
                    "oom": False,
                    "error": "",
                    "accepted": "false",
                    "command": command,
                }
            )
    names = [row["name"][1:] for row in rows]
    if fault == "missing":
        rows.pop()
    if fault == "duplicate":
        rows.append(rows[-1])
    if fault == "exit_type":
        rows[-1]["exit_code"] = "unknown"
    if fault == "command":
        rows[-1]["command"] = ["unknown-program"]
    (tmp_path / "rows").write_text("\n".join(json.dumps(row) for row in rows))
    (tmp_path / "names").write_text("\n".join(names))
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env python3
import os, sys
from pathlib import Path
p = Path(os.environ['INVENTORY_TEST'])
with (p / 'calls').open('a') as out: out.write(sys.argv[1] + '\\n')
if sys.argv[1:3] == ['container', 'ls']: print((p / 'names').read_text())
elif sys.argv[1] == 'inspect':
    if os.environ['FAULT'] == 'daemon': sys.exit(1)
    print((p / 'rows').read_text())
else: sys.exit(99)
""")
    docker.chmod(0o755)
    script = (
        "set -euo pipefail\nFETCHER_DEPLOY_MODE=apply\npilot_state=/checkpoint\n"
        + role
        + "\n"
        + """
runtime_inventory_failure() { exit 17; }
require_full_checkpoint() { echo checked >> "$INVENTORY_TEST/checkpoints"; }
"""
        + inventory
    )
    result = subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        timeout=10,
        env={
            **script_env(),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "INVENTORY_TEST": str(tmp_path),
            "PYTHONOPTIMIZE": "1",
            "FAULT": fault,
        },
    )
    assert (result.returncode == 0) == (fault == ""), result.stderr
    assert (tmp_path / "calls").read_text().splitlines() == ["container", "inspect"]
    if not fault:
        assert len((tmp_path / "checkpoints").read_text().splitlines()) == 8


@pytest.mark.parametrize("failure", ["", "predeploy", "credentials"])
def test_combined_findb_precheck_runs_both_checks_and_stops_on_failure(tmp_path, failure):
    source = (RUNTIME / "deploy_findb_aws.sh").read_text()
    script = source.split("<<'MIGRATION_CHECK_SCRIPT'\n", 1)[1].split(
        "\nMIGRATION_CHECK_SCRIPT", 1
    )[0]
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env bash
set -euo pipefail
while [ "$1" != sh ]; do shift; done
exec "$@"
""")
    python = tmp_path / "python"
    python.write_text("""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$CHECK_LOG"
case "$1:$FAILURE" in
  */predeploy_db_check.py:predeploy|*/reconcile_deployment_credentials.py:credentials) exit 42 ;;
esac
""")
    for command in (docker, python):
        command.chmod(0o755)
    result = subprocess.run(
        ["bash", "-s", "--", "/compose.yml"],
        input=script,
        text=True,
        capture_output=True,
        env={
            **script_env(),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PREDEPLOY_EXPECTED_ALEMBIC_REVISION": "revision",
            "PREDEPLOY_EXPECTED_RDS_ENDPOINT": "db.example.test",
            "APP_ENVIRONMENT": "staging",
            "FINDB_DEPLOY_MODE": "apply",
            "FAILURE": failure,
            "CHECK_LOG": str(tmp_path / "checks"),
        },
    )
    assert result.returncode == (42 if failure else 0)
    calls = (tmp_path / "checks").read_text().splitlines()
    assert "--expected-alembic-revision revision" in calls[0]
    assert "--expected-rds-endpoint db.example.test" in calls[0]
    assert len(calls) == (1 if failure == "predeploy" else 2)
    if len(calls) == 2:
        assert "--deployment-target staging --check-only" in calls[1]
