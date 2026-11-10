"""Exercise runner decisions and crash recovery with a stateful Docker boundary."""

import copy
import importlib.util
import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "infra/deploy"
sys.path.insert(0, str(DEPLOY))
import deployment_receipt as receipt  # noqa: E402
import deployment_timing as timing  # noqa: E402
import deployment_transaction as transaction  # noqa: E402
import release_manifest as manifest_tool  # noqa: E402

SHA = "a" * 64
TX = SHA + "-12-1"
COMMAND = "11111111-1111-1111-1111-111111111111"


def manifest(unit="fetcher", target="staging"):
    account = "439622209937" if target == "staging" else "289112218471"
    return {
        "schema_version": 4,
        "deployment_protocol": "single-pass-v1",
        "unit": unit,
        "deployment_target": target,
        "commit_sha": "b" * 40,
        "images": {
            name: repository + "@sha256:" + "c" * 64
            for name, repository in manifest_tool.image_repositories(
                target, unit, f"{account}.dkr.ecr.ap-southeast-1.amazonaws.com"
            ).items()
        },
        "migration_revision": "none" if unit == "fetcher" else "d" * 12,
        "release_tag": "" if target == "staging" else f"{unit}-v1.0.0",
        "runtime_profile": "unified",
    }


def decision(m=None, tx=TX):
    m = m or manifest()
    prefix = m["unit"] + ("/production" if m["deployment_target"] == "production" else "")
    return {
        **m,
        "state": "accepted",
        "phase": "healthy",
        "transaction_id": tx,
        "bundle_sha256": SHA,
        "bundle_key": f"{prefix}/accepted/{m['commit_sha']}/{SHA}.tar",
        "workflow_run_id": "12",
        "workflow_run_attempt": tx.rsplit("-", 1)[-1],
        "ssm_command_id": COMMAND,
    }


@pytest.mark.parametrize("unit", ["findb", "fetcher"])
@pytest.mark.parametrize("target", ["staging", "production"])
def test_exact_decision_binds_all_release_fields(unit, target):
    m = manifest(unit, target)
    record = decision(m)
    receipt.validate(record, m, bundle_sha=SHA, transaction_id=TX)
    for key in (
        "unit",
        "deployment_target",
        "commit_sha",
        "images",
        "migration_revision",
        "release_tag",
        "bundle_key",
        "bundle_sha256",
        "phase",
        "deployment_protocol",
        "ssm_command_id",
        "workflow_run_id",
    ):
        invalid = {**record, key: "wrong"}
        with pytest.raises(ValueError):
            receipt.validate(invalid, m, bundle_sha=SHA, transaction_id=TX)


def test_previous_acceptance_is_not_a_decision_for_replay():
    previous = decision(tx=SHA + "-12-2")
    receipt.validate(previous, manifest(), bundle_sha=SHA)
    with pytest.raises(ValueError, match="another attempt"):
        receipt.validate(previous, manifest(), bundle_sha=SHA, transaction_id=TX)


def row(name, *, original=False):
    return {
        "Id": ("old-" if original else "new-") + name,
        "Name": "/" + name,
        "Config": {
            "Image": "old-image" if original else "new-image",
            "Labels": {} if original else {transaction.LABEL: TX},
            "Env": ["SECRET=must-not-be-journaled"],
        },
        "State": {
            "Running": True,
            "Status": "running",
            "ExitCode": 0,
            "OOMKilled": False,
            "Error": "",
        },
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped" if original else "no"}},
        "RestartCount": 0,
    }


@pytest.fixture
def host(tmp_path, monkeypatch):
    tx = object.__new__(transaction.Transaction)
    tx.root = tmp_path / "releases" / TX
    tx.root.mkdir(parents=True)
    tx.base = tmp_path
    tx.unit, tx.tx = "fetcher", TX
    tx.names = transaction.FETCHER_NAMES
    tx.manifest = manifest()
    tx.directory = tmp_path / "transactions" / TX
    tx.directory.mkdir(parents=True)
    tx.path = tx.directory / "journal.json"
    tx.timer = "findb-deployment-recovery-fetcher"
    old_root = tmp_path / "releases" / "previous"
    old_root.mkdir()
    (tmp_path / "current").symlink_to(old_root)
    originals = [row(name, original=True) for name in tx.names]
    state = {"rows": copy.deepcopy(originals), "calls": [], "decision": decision()}
    journal = {
        "phase": "healthy",
        "unit": tx.unit,
        "transaction_id": TX,
        "previous_root": str(old_root),
        "originals": [transaction.summary(r) for r in originals],
        "healthy": [],
        "bucket": "test-bucket",
        "deadline": 0,
        "boot_id": "original-boot",
    }
    for r in state["rows"]:
        r["State"].update(Running=False, Status="exited")
        r["Name"] += "-previous"
    state["rows"].extend(row(n) for n in tx.names)
    journal["healthy"] = transaction.healthy(state["rows"], tx.names, TX)
    tx.save(journal)
    monkeypatch.setattr(tx, "load", lambda: json.loads(tx.path.read_text()))
    monkeypatch.setattr(tx, "lock", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(transaction, "inventory", lambda: copy.deepcopy(state["rows"]))
    monkeypatch.setattr(transaction, "boot_id", lambda: "original-boot")

    def command(*args, **kwargs):
        state["calls"].append(args)
        if args[0] == "aws":
            if isinstance(state["decision"], Exception):
                raise state["decision"]
            return json.dumps(state["decision"])
        assert args[0] == "docker"
        op = args[1]
        if op == "update":
            for r in state["rows"]:
                if r["Id"] in args[4:]:
                    r["HostConfig"]["RestartPolicy"]["Name"] = args[3]
            return ""
        r = next((r for r in state["rows"] if r["Id"] == args[-1]), None)
        if op == "rename":
            r = next(r for r in state["rows"] if r["Id"] == args[2])
            r["Name"] = "/" + args[3]
        elif op == "inspect":
            return json.dumps([r])
        elif op in ("stop", "start"):
            r["State"].update(
                Running=op == "start", Status="running" if op == "start" else "exited"
            )
        elif op == "rm":
            assert not r["State"]["Running"]
            state["rows"].remove(r)
        else:
            raise AssertionError(args)
        return ""

    monkeypatch.setattr(transaction, "run", command)
    return tx, journal, state


def test_finalize_never_restarts_or_recreates_healthy_consumers(host):
    tx, journal, state = host
    tx.finalize_locked(journal)
    assert tx.load()["phase"] == "committed"
    assert (tx.base / "current").readlink() == tx.root
    assert len(state["rows"]) == 8
    assert all(r["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped" for r in state["rows"])
    assert not any(
        c[0:2] in (("docker", "start"), ("docker", "run"), ("docker", "stop"))
        for c in state["calls"]
    )
    assert "must-not-be-journaled" not in tx.path.read_text()
    tx.finalize_locked(tx.load())  # idempotent


@pytest.mark.parametrize("change", ["missing", "restarted", "replaced", "wrong_image", "oom"])
def test_changed_healthy_identity_cannot_be_committed(host, change):
    tx, journal, state = host
    target = state["rows"][-1]
    if change == "missing":
        state["rows"].pop()
    if change == "restarted":
        target["RestartCount"] = 1
    if change == "replaced":
        target["Id"] = "impostor"
    if change == "wrong_image":
        target["Config"]["Image"] = "impostor"
    if change == "oom":
        target["State"]["OOMKilled"] = True
    with pytest.raises(ValueError):
        tx.finalize_locked(journal)
    assert tx.load()["phase"] == "healthy"
    assert not any(c[:2] == ("docker", "rm") for c in state["calls"])


@pytest.mark.parametrize(
    "failure", [RuntimeError("network unavailable"), decision(tx=SHA + "-12-2")]
)
def test_missing_or_old_decision_restores_all_twelve_originals(host, failure):
    tx, journal, state = host
    state["decision"] = failure
    tx.recover()
    assert tx.load()["phase"] == "aborted"
    assert {(r["Id"], r["Name"]) for r in state["rows"]} == {
        (r["id"], "/" + r["name"]) for r in journal["originals"]
    }
    assert all(r["State"]["Running"] for r in state["rows"])
    assert (tx.base / "current").readlink() == Path(journal["previous_root"])


def test_watchdog_finishes_durable_acceptance_without_second_activation(host):
    tx, _, state = host
    tx.recover()
    assert tx.load()["phase"] == "committed"
    assert not any(c[:2] == ("docker", "run") for c in state["calls"])


def test_watchdog_waits_for_live_lease_but_recovers_after_reboot(host, monkeypatch):
    tx, journal, state = host
    journal["deadline"] = transaction.time.time() + 120
    tx.save(journal)
    tx.recover()
    assert not state["calls"]
    state["decision"] = RuntimeError("offline")
    monkeypatch.setattr(transaction, "boot_id", lambda: "new-boot")
    tx.recover()
    assert tx.load()["phase"] == "aborted"


def test_partial_apply_rollback_does_not_touch_unrelated_container(host):
    tx, journal, state = host
    unrelated = row("unrelated", original=True)
    state["rows"].append(unrelated)
    journal["phase"] = "applying"
    tx.abort_locked(journal)
    assert unrelated in state["rows"]
    assert not any("old-unrelated" in c for c in state["calls"])


def test_journal_is_atomic_private_and_does_not_reuse_stale_temp(tmp_path):
    path = tmp_path / "journal.json"
    path.with_suffix(".tmp").write_text("interrupted")
    transaction.atomic_json(path, {"phase": "healthy"})
    transaction.atomic_json(path, {"phase": "committed"})
    assert json.loads(path.read_text()) == {"phase": "committed"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_timing_subtracts_only_exclusive_wait_and_verify():
    def iso(second):
        return f"2026-11-09T00:{second // 60:02}:{second % 60:02}Z"

    def job(name, start, end, steps=()):
        return {
            "name": name,
            "started_at": iso(start),
            "completed_at": iso(end),
            "conclusion": "success",
            "steps": list(steps),
        }

    wait = {
        "name": "Wait for same-revision FinDB staging rollout",
        "started_at": iso(100),
        "completed_at": iso(180),
        "conclusion": "success",
    }
    jobs = [
        job("verify / backend", 0, 100),
        job("verify / migrations", 0, 60),
        job("publish", 40, 80),
        job("deploy / deploy", 100, 250, [wait]),
    ]
    result = timing.report(jobs)
    assert result["wall_seconds"] == 250
    assert result["verify_seconds"] == 60
    assert result["dependency_wait_seconds"] == 80
    assert result["deployment_seconds"] == 110
    jobs[-1]["conclusion"] = "failure"
    assert timing.report(jobs)["status"] == "failed"


def test_new_manifest_bundles_protocol_tools_and_preserves_v3():
    assert "infra/deploy/deployment_transaction.py" not in manifest_tool.bundle_paths("fetcher", 3)
    assert "infra/deploy/deployment_transaction.py" in manifest_tool.bundle_paths("fetcher", 4)
    contracts = manifest_tool.load_contract_manifest(ROOT / "contracts/manifest.json")
    m = manifest()
    m.update(
        account_id="439622209937",
        registry=manifest_tool.REGISTRY,
        fetcher_bundle_version=5,
        created_by_run_id="12",
        contract_versions=list(contracts.versions),
        contract_manifest_sha256=contracts.canonical_sha256,
    )
    with manifest_tool.SourceBundleReader(ROOT) as source:
        m["deployment_source_bundle_sha256"] = source.deployment_source_bundle_sha256("fetcher", 4)
        manifest_tool.validate_manifest(m, contracts, source)
    m["deployment_protocol"] = "unknown"
    with pytest.raises(manifest_tool.ManifestError):
        manifest_tool.validate_manifest(m, contracts)


def test_normal_post_findb_recovery_has_no_fixed_wait():
    spec = importlib.util.spec_from_file_location(
        "recovery_for_single_pass", DEPLOY / "check_fetcher_runtime_recovery.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    snapshot = (("name", "id", "image", 0, "start", True),)
    assert (
        module.wait_until_recovered(
            lambda: snapshot,
            stable_seconds=0,
            timeout_seconds=180,
            poll_seconds=5,
            sleep=calls.append,
        )
        == snapshot
    )
    assert calls == []


@pytest.mark.parametrize("failed_put", [1, 2, 3])
def test_runner_does_not_publish_decision_after_storage_failure(tmp_path, monkeypatch, failed_put):
    m = manifest()
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(m))
    bundle = tmp_path / "bundle.tar"
    bundle.write_bytes(b"bundle")
    calls = []

    def aws(args, **kwargs):
        calls.append(args)
        if len(calls) == failed_put:
            return subprocess.CompletedProcess(args, 1, "", '{"Code":"AccessDenied"}')
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", aws)
    with pytest.raises(RuntimeError):
        receipt.publish(
            manifest_path=mp,
            bundle_path=bundle,
            bucket="bucket",
            kms_key="kms",
            command_id=COMMAND,
            validator_sha="e" * 64,
            run_id="12",
            attempt="1",
        )
    assert len(calls) == failed_put
    if failed_put < 3:
        assert all("/transactions/" not in call[call.index("--key") + 1] for call in calls)


@pytest.mark.parametrize("change", ["id", "image", "started_at"])
def test_common_observation_rejects_replacement_during_window(host, monkeypatch, change):
    tx, _, state = host
    clock = [0.0]
    monkeypatch.setattr(transaction.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        clock[0] += seconds
        target = state["rows"][-1]
        if change == "id":
            target["Id"] = "replacement"
        elif change == "image":
            target["Config"]["Image"] = "replacement"
        else:
            target["State"]["StartedAt"] = "replacement"

    monkeypatch.setattr(transaction.time, "sleep", sleep)
    with pytest.raises(ValueError, match="identity changed"):
        transaction.observe(tx.names, tx.tx, 25)


@pytest.mark.parametrize("compatible", [True, False])
def test_findb_recovery_uses_previous_exact_revision_without_downgrade(
    host, monkeypatch, compatible
):
    tx, journal, state = host
    tx.unit = "findb"
    journal["unit"] = "findb"
    journal["environment"] = {"APP_ENVIRONMENT": "staging"}
    previous = manifest("findb")
    (Path(journal["previous_root"]) / "release-manifest.json").write_text(json.dumps(previous))
    restore_environments = []

    def execute(command, env, journal):
        assert command.name == "deploy_findb_aws.sh"
        assert journal["phase"] == "restoring"
        restore_environments.append(env)
        if not compatible:
            raise RuntimeError("database revision incompatible")

    monkeypatch.setattr(tx, "execute_host", execute)
    tx.abort_locked(journal)
    assert tx.load()["phase"] == ("aborted" if compatible else "recovery_required")
    assert not any(transaction.owned(r, TX) for r in state["rows"])
    assert len(restore_environments) == 1
    env = restore_environments[0]
    assert env["FINDB_DEPLOY_MODE"] == "activate"
    assert env["PREDEPLOY_EXPECTED_ALEMBIC_REVISION"] == previous["migration_revision"]
    assert "FINDB_TRANSACTION_COMPOSE_FILE" not in env
    assert env["FINDB_IMAGE_REF"] == previous["images"]["backend"]


@pytest.mark.parametrize(
    "module_name", ["deployment_receipt", "check_fetcher_host", "check_fetcher_runtime_recovery"]
)
def test_v4_host_evidence_requires_committed_receipt_and_exact_container(
    tmp_path, monkeypatch, module_name
):
    import importlib
    from types import SimpleNamespace

    module = importlib.import_module(module_name)
    directory = tmp_path / "transactions" / TX
    directory.mkdir(parents=True, mode=0o700)
    target = row("consumer")
    journal = {"phase": "healthy", "healthy": [transaction.summary(target)]}
    transaction.atomic_json(directory / "journal.json", journal)
    transaction.atomic_json(directory / "receipt.json", decision())
    original_lstat = Path.lstat

    def root_owned(path):
        info = original_lstat(path)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0)

    monkeypatch.setattr(Path, "lstat", root_owned)
    assert not module.committed_runtime(target, tmp_path)
    journal["phase"] = "committed"
    transaction.atomic_json(directory / "journal.json", journal)
    assert module.committed_runtime(target, tmp_path)
    target["Id"] = "unknown"
    assert not module.committed_runtime(target, tmp_path)
    target["Id"] = journal["healthy"][0]["id"]
    transaction.atomic_json(directory / "receipt.json", {**decision(), "transaction_id": "other"})
    assert not module.committed_runtime(target, tmp_path)


@pytest.mark.parametrize("fail_backend", [False, True])
def test_parallel_publication_waits_for_both_images_before_exposing_outputs(tmp_path, fail_backend):
    import os

    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/findb-cd.yml").read_text())
    step = next(s for s in workflow["jobs"]["publish"]["steps"] if s.get("id") == "images")
    helper = tmp_path / "infra/deploy/runtime-secrets/build_ecr_image_if_missing.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text("""#!/bin/bash
set -euo pipefail
name="${1##*/}"
touch "$RUNNER_TEMP/$name-started"
# Neither helper can finish until both have started: serial execution fails.
for attempt in {1..200}; do
  if [ -f "$RUNNER_TEMP/backend-started" ] && [ -f "$RUNNER_TEMP/dashboard-started" ]; then break; fi
  sleep 0.01
done
[ -f "$RUNNER_TEMP/backend-started" ] && [ -f "$RUNNER_TEMP/dashboard-started" ]
if [ "$name" = backend ] && [ "$FAIL_BACKEND" = true ]; then exit 7; fi
printf 'image_ref=%s@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n' "$1" >> "$GITHUB_OUTPUT"
""")
    helper.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, script in (
        ("aws", '#!/bin/sh\necho "$ACCOUNT"\n'),
        ("docker", "#!/bin/sh\ncat >/dev/null\n"),
    ):
        executable = bindir / name
        executable.write_text(script)
        executable.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=tmp_path,
        env={
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "ACCOUNT": "439622209937",
            "AWS_REGION": "ap-southeast-1",
            "REGISTRY": "registry",
            "BACKEND": "registry/backend",
            "DASHBOARD": "registry/dashboard",
            "FAIL_BACKEND": str(fail_backend).lower(),
        },
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode != 0) == fail_backend, result.stderr
    assert (tmp_path / "backend-started").exists()
    assert (tmp_path / "dashboard-started").exists()
    if fail_backend:
        assert not output.exists()
    else:
        assert len(output.read_text().splitlines()) == 2


@pytest.mark.parametrize("original", [True, False])
def test_unclean_shutdown_blocks_automatic_restart_and_requires_recovery(host, original):
    tx, journal, state = host
    target = state["rows"][0 if original else -1]
    target["State"]["ExitCode"] = 137
    with pytest.raises(ValueError, match="shutdown not graceful"):
        tx.abort_locked(journal)
    assert tx.load()["phase"] == "recovery_required"
    assert not any(call[:2] == ("docker", "start") for call in state["calls"])


def test_transaction_owns_only_full_market_and_historical_consumers():
    assert len(transaction.FETCHER_NAMES) == 8
    assert not any("pilot" in name for name in transaction.FETCHER_NAMES)
    assert {name.split("-")[1] for name in transaction.FETCHER_NAMES} == {"full", "historical"}
    # The retired flag is never journaled or replayed into an older release.
    assert "FULL_MARKET_ENABLED" not in transaction.ENV_NAMES
    assert "PROVIDER_WORK_REQUIRED" in transaction.ENV_NAMES
