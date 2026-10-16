"""Execute the deployment coordinator against a stateful fake Docker daemon.

Only /opt/fetcher paths are relocated into a temporary directory. The actual
validation, ERR trap, retirement, candidate and rollback shell paths execute.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh"
REGISTRY = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
PROVIDERS = tuple(
    (
        f"findb-{profile.replace('_', '-')}-{provider.replace('_', '-')}",
        "twelve-data" if provider == "taifex" else provider.replace("_", "-"),
        provider,
        profile,
    )
    for profile in ("pilot", "full_market", "historical")
    for provider in ("twelve_data", "finlab", "shioaji", "taifex")
    if profile != "historical" or provider != "taifex"
)

FAKE_COMMAND = r"""
import json, os, sys
from pathlib import Path
store = Path(os.environ["FAKE_DOCKER_STATE"])
state = json.loads(store.read_text())
rows = state["rows"]
args = sys.argv[1:]
command = Path(sys.argv[0]).name
state["log"].append([command, *args])
status = 0
if command == "commit-boundary": state["committed"] = True
elif command == "sleep": pass
elif command == "stat": print("root:root:700")
elif command == "mv": os.replace(args[-2], args[-1])
elif command == "sudo":
    # Runtime secret wrapper itself is not under test. No credentials are used.
    if args[0] == "test":
        path=Path(args[-1]);status=0 if (path.is_file() if args[1] == "-f" else path.is_symlink()) else 1
        store.write_text(json.dumps(state));raise SystemExit(status)
    if args[0] == "python3":
        import subprocess
        result=subprocess.run([sys.executable,*args[1:]],input=sys.stdin.read(),text=True,capture_output=True)
        store.write_text(json.dumps(state));raise SystemExit(result.returncode)
    if args[args.index("--consumer") + 1] == "images":
        state["images_prepared"] = True
        store.write_text(json.dumps(state));raise SystemExit(0)
    index = next(i for i, a in enumerate(args) if a.endswith("/release_fetcher_provider.sh"))
    provider, image, state_dir, state_path, stable, candidate, previous = args[index+1:index+8]
    state.setdefault("provider_images", []).append([stable, image])
    runtime_command = args[index+10:]
    if state.get("fault") == "provider_before": status = 7
    else:
        row = rows.get(stable)
        if row and row.get("accepted") == "false":
            rows.pop(stable);row=None
        if row is None and previous in rows:
            rows[stable]=rows.pop(previous);row=rows[stable];row["running"]=True
        if row:
            row["running"] = False
            row["exit"] = row.get("stop_exit", 0)
            if row["exit"] != 0: status = 8
            elif state.get("fault") == "rename_once":
                state["fault"] = "used";status = 5
            else: rows[previous] = rows.pop(stable)
        if not status and os.environ["FETCHER_DEPLOY_MODE"] == "activate":
            rows[stable] = dict(id="new-"+stable, image=image, running=True, exit=0, oom=False, error="", accepted="true", cmd=runtime_command)
            if os.environ["APP_ENVIRONMENT"] == "production" and os.environ.get("FETCHER_RUNTIME_PROFILE", "bounded") == "bounded":
                rows[stable+"-historical"] = dict(id="new-"+stable+"-historical", running=True, exit=0, oom=False, error="", accepted="true", marker="date-boundary-v1", cmd=["findb-fetch-historical-backfill","--provider",provider.replace("-","_"),"--run-forever"])
        if not status and state.get("fault") == "provider_after": status = 9
        if not status and state.get("fault") == "provider_signal": state["signal_on_exit"] = True
elif command == "docker":
    if args[:2] == ["image", "prune"]: pass
    elif args[:2] == ["container", "ls"]:
        if state.get("fault", "").startswith("inventory-list-"): status = int(state["fault"].rsplit("-", 1)[1])
        else: print("\n".join(rows))
    elif args[0] == "container":
        if state.get("fault", "").startswith("inventory-inspect:") and args[-1] == state["fault"].split(":")[1]: status = int(state["fault"].split(":")[2])
        else: status = 0 if args[-1] in rows else 1
    elif args[0] == "inspect":
        row = rows.get(args[-1])
        if row is None: row = next((row for row in rows.values() if row["id"] == args[-1]), None)
        if row is None: status = 1
        else:
            template = args[2]
            field_fault = state.get("postcommit_field") if state.get("committed") else None
            if field_fault and args[-1].startswith("old-") and field_fault[0] in template:
                if field_fault[1] in ["match-rc1", "match-rc81"]:
                    print(row["id"] if field_fault[0] == ".Id" else row.get("image", "new"))
                    status = int(field_fault[1].rsplit("rc", 1)[1])
                elif field_fault[1] in ["rc1", "rc81"]: status = int(field_fault[1][2:])
                else: print(field_fault[1])
            elif ".State.Running" in template: print(str(row["running"]).lower())
            elif ".RestartCount" in template: print(1 if state.get("fault") == "activation-restarts" else 0)
            elif ".State.ExitCode" in template: print(row["exit"])
            elif ".State.OOMKilled" in template: print(str(row["oom"]).lower())
            elif ".State.Error" in template: print(row["error"])
            elif ".Id" in template: print(row["id"])
            elif ".Config.Image" in template: print(row.get("image", "new"))
            elif template == "{{json .Config.Cmd}}" and state.get("command_query") and args[-1] == state["command_query"][0]:
                print(state["command_query"][1])
            elif ".Config.Cmd" in template:
                command=row["cmd"] if isinstance(row.get("cmd"),list) else row.get("cmd", "").split()
                print(json.dumps(command,separators=(",", ":")) if template == "{{json .Config.Cmd}}" else " ".join(command))
            elif ".Path" in template: print(row.get("path", "findb-fetch-historical-backfill"))
            elif ".Args" in template: print(json.dumps(row.get("args", row.get("cmd", "").split()[1:]), separators=(",", ":")))
            elif "historical-shutdown" in template: print("present" if "marker" in row else "")
            elif "accepted" in template: print(row["accepted"])
            else: status = 99
            fault = state.get("fault", "").split(":")
            if len(fault) == 4 and fault[0] == "initial-field" and args[-1] == fault[1] and fault[2] in template:
                status = int(fault[3][2:])
    elif args[0] == "stop":
        row = rows[args[-1]]
        if row.get("stop_error"): status = 6
        else:
            row["running"] = False
            row["exit"] = row.get("stop_exit", 0)
    elif args[0] == "start":
        state.setdefault("started_ids", []).append(rows[args[-1]]["id"])
        rows[args[-1]]["running"] = True
    elif args[0] == "rm":
        name = args[-1]
        if name not in rows: name = next(n for n,r in rows.items() if r["id"] == name)
        if rows[name]["running"]: status = 81
        else: rows.pop(name)
    elif args[0] == "rename":
        if state.get("fault") == "rename_once":
            state["fault"] = "used"
            status = 5
        else: rows[args[2]] = rows.pop(args[1])
    else: status = 99
else: status = 99
send=state.pop("signal_on_exit",False)
store.write_text(json.dumps(state))
if send:
    import signal
    os.kill(os.getppid(), signal.SIGTERM)
raise SystemExit(status)
"""


def original_rows():
    return {
        stable: dict(
            id="old-" + stable,
            running=True,
            exit=0,
            oom=False,
            error="",
            accepted="true",
            cmd=(
                f"findb-fetch-historical-backfill --provider {provider} --run-forever"
                if profile == "historical"
                else f"findb-fetch-runtime --execution-profile {profile} --provider {provider} --run-forever"
            ),
        )
        for stable, repository, provider, profile in PROVIDERS
    }


def execute(
    rows,
    *,
    mode="candidate",
    fault="",
    profile="unified",
    target="staging",
    source=None,
    postcommit_field=None,
    full_checkpoint=True,
    accepted_replay=False,
    command_query=None,
    checkpoint_kind="valid",
):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        host = root / "fetcher"
        release = host / "releases" / ("a" * 64 + "-123-1")
        old_release = host / "releases" / ("b" * 64 + "-122-1")
        old_release.mkdir(parents=True)
        runtime = release / "infra/deploy/runtime-secrets"
        runtime.mkdir(parents=True)
        for name in (
            "fetcher.json",
            "runtime_secret_command.sh",
            "release_fetcher_provider.sh",
        ):
            (runtime / name).write_text("{}")
        (release / "release-manifest.json").write_text(json.dumps({"runtime_profile": profile}))
        (host / "current").symlink_to(old_release)
        script = root / "deploy.sh"
        script_source = source or DEPLOY.read_text()
        checkpoint = root / "checkpoint"
        if full_checkpoint:
            import sqlite3

            state_dir = checkpoint / "staging" if target == "staging" else checkpoint
            state_dir.mkdir(parents=True)
            with sqlite3.connect(state_dir / "state.sqlite3") as db:
                db.executescript(
                    (ROOT / "fetcher/tests/fixtures/full_market_legacy_core.sql").read_text()
                )
            if checkpoint_kind == "corrupt":
                with sqlite3.connect(state_dir / "state.sqlite3") as db:
                    page = db.execute(
                        "SELECT rootpage FROM sqlite_master WHERE name='full_work'"
                    ).fetchone()[0]
                    size = db.execute("PRAGMA page_size").fetchone()[0]
                with (state_dir / "state.sqlite3").open("r+b") as raw:
                    raw.seek((page - 1) * size)
                    raw.write(b"\0")
            elif checkpoint_kind == "unusable":
                with sqlite3.connect(state_dir / "state.sqlite3") as db:
                    db.executescript("DROP TABLE full_work; CREATE TABLE full_work(unusable TEXT);")
            script_source = script_source.replace("/var/lib/findb-full-market", str(checkpoint))
        # Pilot has an independent checkpoint; never share the full path.
        pilot_root = root / "pilot-checkpoint"
        if full_checkpoint:
            import shutil

            pilot_dir = pilot_root / target
            pilot_dir.mkdir(parents=True)
            shutil.copyfile(state_dir / "state.sqlite3", pilot_dir / "state.sqlite3")
        script_source = script_source.replace("/var/lib/findb-pilot", str(pilot_root))
        checkpoint_bytes = (state_dir / "state.sqlite3").read_bytes() if full_checkpoint else None
        if postcommit_field:
            script_source = script_source.replace(
                "trap - ERR INT TERM HUP\nif ! cleanup_originals",
                "trap - ERR INT TERM HUP\ncommit-boundary\nif ! cleanup_originals",
            )
        script.write_text(script_source.replace("/opt/fetcher", str(host)))
        state = root / "state.json"
        state.write_text(
            json.dumps(
                {
                    "rows": rows,
                    "log": [],
                    "fault": fault,
                    "postcommit_field": postcommit_field,
                    "command_query": command_query,
                }
            )
        )
        binaries = root / "bin"
        binaries.mkdir()
        for name in ("docker", "sudo", "stat", "mv", "commit-boundary", "sleep"):
            path = binaries / name
            path.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
            path.chmod(0o700)
        account = "439622209937" if target == "staging" else "289112218471"
        registry = f"{account}.dkr.ecr.ap-southeast-1.amazonaws.com"
        environment = {
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "FAKE_DOCKER_STATE": str(state),
            "AWS_REGION": "ap-southeast-1",
            "AWS_ACCOUNT_ID": account,
            "APP_ENVIRONMENT": target,
            "ECR_REGISTRY": registry,
            "FETCHER_RELEASE_ROOT": str(release),
            "FETCHER_DEPLOY_MODE": mode,
            "FETCHER_RUNTIME_PROFILE": profile,
            "FETCHER_ACCEPTED_REPLAY": str(accepted_replay).lower(),
            "TWELVE_IMAGE_REF": f"{registry}/findb/{target}/fetcher/twelve-data@sha256:{'c' * 64}",
            "FINLAB_IMAGE_REF": f"{registry}/findb/{target}/fetcher/finlab@sha256:{'c' * 64}",
            "SHIOAJI_IMAGE_REF": f"{registry}/findb/{target}/fetcher/shioaji@sha256:{'c' * 64}",
        }
        completed = subprocess.run(
            ["bash", str(script)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        actual = json.loads(state.read_text())
        if full_checkpoint:
            actual["checkpoint_preserved"] = (
                state_dir / "state.sqlite3"
            ).read_bytes() == checkpoint_bytes
        return (
            completed,
            actual,
            (host / "current").readlink().name,
        )


def assert_restored(before, state):
    assert set(state["rows"]) == set(before)
    for name, original in before.items():
        assert state["rows"][name]["id"] == original["id"]
        assert state["rows"][name]["running"] == original["running"]
    assert state["checkpoint_preserved"]


@pytest.mark.parametrize("mode", ["candidate", "activate"])
@pytest.mark.parametrize(
    "fault", ["provider_before", "provider_after", "provider_signal", "rename_once"]
)
def test_failed_provider_transaction_preserves_originals_and_pointer(mode, fault):
    before = original_rows()
    completed, state, pointer = execute(before, mode=mode, fault=fault)
    assert completed.returncode != 0
    assert "transaction_aborted" in completed.stderr
    assert_restored(before, state)
    assert pointer == "b" * 64 + "-122-1"


@pytest.mark.parametrize("mode", ["candidate", "activate"])
@pytest.mark.parametrize("boundary", ["before", "after"])
@pytest.mark.parametrize("signum,code", [("TERM", 143), ("INT", 130), ("HUP", 129)])
def test_signal_at_registration_preserves_originals(mode, boundary, signum, code):
    source = DEPLOY.read_text()
    registration = '  processed+=("${stable}:${previous}:${old_available}:${original_id}")'
    second = PROVIDERS[1][0]
    injection = f'  if [ "$stable" = {second} ]; then kill -{signum} "$$"; fi'
    replacement = (
        injection + "\n" + registration if boundary == "before" else registration + "\n" + injection
    )
    before = original_rows()
    completed, state, pointer = execute(
        before, mode=mode, source=source.replace(registration, replacement)
    )
    assert completed.returncode == code, completed.stderr
    assert_restored(before, state)
    assert pointer == "b" * 64 + "-122-1"


@pytest.mark.parametrize("kind", ["corrupt", "unusable"])
def test_invalid_checkpoint_has_no_runtime_effects(kind):
    completed, state, _ = execute(original_rows(), checkpoint_kind=kind)
    assert completed.returncode != 0
    assert "checkpoint_invalid" in completed.stderr
    assert not state.get("provider_images")
    assert not any(
        call[:2] in (["docker", "stop"], ["docker", "rename"], ["docker", "rm"])
        for call in state["log"]
    )


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
def test_missing_installed_checkpoint_does_not_bootstrap(profile):
    name = next(name for name, _, _, item in PROVIDERS if item == profile)
    completed, state, _ = execute({name: original_rows()[name]}, full_checkpoint=False)
    assert completed.returncode != 0
    assert "checkpoint_missing" in completed.stderr
    assert not state.get("provider_images")


@pytest.mark.parametrize(
    "fault", ["inventory-list-1", "inventory-list-81", "inventory-inspect:findb-pilot-finlab:1"]
)
def test_unknown_inventory_is_not_absence(fault):
    completed, state, _ = execute(original_rows(), fault=fault)
    assert completed.returncode != 0
    assert "query_unknown" in completed.stderr
    assert not state.get("provider_images")


@pytest.mark.parametrize(
    "field,value",
    [(".Id", "wrong"), (".Id", "rc1"), (".Config.Image", "wrong"), (".Config.Image", "match-rc81")],
)
def test_postcommit_identity_failure_never_deletes_originals(field, value):
    before = original_rows()
    completed, state, pointer = execute(before, mode="activate", postcommit_field=(field, value))
    assert completed.returncode != 0
    assert "committed_cleanup_failed committed=true" in completed.stderr
    assert pointer == "a" * 64 + "-123-1"
    assert {row["id"] for row in before.values()} <= {row["id"] for row in state["rows"].values()}
    assert not any(call[:2] == ["docker", "rm"] for call in state["log"])


@pytest.mark.parametrize("mode", ["candidate", "activate"])
def test_both_profiles_have_same_deployment_path(mode):
    before = original_rows()
    completed, state, pointer = execute(before, mode=mode)
    assert completed.returncode == 0, completed.stderr
    assert {name for name, _ in state["provider_images"]} == set(before)
    if mode == "candidate":
        assert_restored(before, state)
        assert pointer == "b" * 64 + "-122-1"
    else:
        assert pointer == "a" * 64 + "-123-1"
        assert set(state["rows"]) == set(before)
        assert all(row["id"].startswith("new-") for row in state["rows"].values())


@pytest.mark.parametrize(
    "change", [{"stop_exit": 137}, {"stop_exit": 1}, {"oom": True}, {"error": "private-error"}]
)
def test_unclean_stop_fails_without_deleting_the_original(change):
    before = original_rows()
    name = PROVIDERS[0][0]
    before[name].update(change)
    completed, state, pointer = execute(before, mode="activate")
    assert completed.returncode != 0
    assert any(row["id"] == before[name]["id"] for row in state["rows"].values())
    assert "private-error" not in completed.stderr
    assert pointer == "b" * 64 + "-122-1"


def test_shared_activation_observation_keeps_all_originals_until_healthy():
    result, state, _ = execute(original_rows(), mode="activate")
    assert result.returncode == 0, result.stderr
    sleeps = [entry for entry in state["log"] if entry[0] == "sleep"]
    assert sleeps == [["sleep", "5"]] * 5
    checks = [
        entry
        for entry in state["log"]
        if entry[0] == "docker" and ".RestartCount" in " ".join(entry)
    ]
    assert len(checks) == len(PROVIDERS) * 6
    assert state["images_prepared"] is True
    wrappers = [entry for entry in state["log"] if entry[0] == "sudo" and "--consumer" in entry]
    assert sum(entry[entry.index("--consumer") + 1] == "images" for entry in wrappers) == 1
    providers = [entry for entry in wrappers if entry[entry.index("--consumer") + 1] != "images"]
    assert len(providers) == 11
    assert all("--docker-login" not in entry for entry in providers)
    last_observation = max(i for i, entry in enumerate(state["log"]) if entry[0] == "sleep")
    first_retirement = next(
        i for i, entry in enumerate(state["log"]) if entry[:2] == ["docker", "rm"]
    )
    assert last_observation < first_retirement
    assert state["log"][-1] == ["docker", "image", "prune", "-af"]


def test_shared_health_failure_restores_original_identities_and_pointer():
    original = original_rows()
    result, state, pointer = execute(original, mode="activate", fault="activation-restarts")
    assert result.returncode != 0
    assert "reason=activation_unhealthy runtime=" in result.stderr
    assert pointer == "b" * 64 + "-122-1"
    assert {name: row["id"] for name, row in state["rows"].items()} == {
        name: row["id"] for name, row in original.items()
    }
    assert all(row["running"] for row in state["rows"].values())
