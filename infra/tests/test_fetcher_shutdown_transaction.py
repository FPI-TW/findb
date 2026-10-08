"""Execute the deployment coordinator against a stateful fake Docker daemon.

Only /opt/fetcher paths are relocated into a temporary directory. The actual
validation, ERR trap, retirement, candidate and rollback shell paths execute.
"""

import copy
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
PROVIDERS = (
    (
        "findb-fetcher-scheduler",
        "twelve-data",
        "twelve_data",
        "2f64ab8c40e082e6601a00839b2345c35c504afcf1d453026d779ac151f46d03",
    ),
    (
        "findb-fetcher-finlab-scheduler",
        "finlab",
        "finlab",
        "bcd882dff3412a55f14921474d16ca27950dc51f37252fae404b03eccb862bc2",
    ),
    (
        "findb-fetcher-shioaji-scheduler",
        "shioaji",
        "shioaji",
        "161991e5b056becbdc35d67c547a7092807f1e0aa66a34b7b921ed57ecc6bd13",
    ),
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
            else: rows[previous] = rows.pop(stable)
        if not status and os.environ["FETCHER_DEPLOY_MODE"] == "activate":
            rows[stable] = dict(id="new-"+stable, running=True, exit=0, oom=False, error="", accepted="true", cmd=runtime_command)
            if os.environ["APP_ENVIRONMENT"] == "production" and os.environ.get("FETCHER_RUNTIME_PROFILE", "bounded") == "bounded":
                rows[stable+"-historical"] = dict(id="new-"+stable+"-historical", running=True, exit=0, oom=False, error="", accepted="true", marker="date-boundary-v1", cmd=["findb-fetch-historical-backfill","--provider",provider.replace("-","_"),"--run-forever"])
        if not status and state.get("fault") == "provider_after": status = 9
        if not status and state.get("fault") == "provider_signal": state["signal_on_exit"] = True
elif command == "docker":
    if args[:2] == ["container", "ls"]:
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
    rows = {}
    for stable, repository, provider, digest in PROVIDERS:
        rows[stable] = dict(
            id="old-" + stable,
            running=True,
            exit=0,
            oom=False,
            error="",
            accepted="true",
            cmd="findb-fetch-scheduler"
            if provider == "twelve_data"
            else f"findb-fetch-{provider}-scheduler",
        )
        rows[stable + "-historical"] = dict(
            id="old-" + stable + "-historical",
            running=True,
            exit=0,
            stop_exit=137,
            oom=False,
            error="",
            accepted="true",
            image=f"{REGISTRY}/findb/staging/fetcher/{repository}@sha256:{digest}",
            cmd=f"findb-fetch-historical-backfill --provider {provider} --run-forever",
        )
    return rows


def execute(
    rows,
    *,
    mode="candidate",
    fault="",
    profile="bounded",
    target="staging",
    source=None,
    postcommit_field=None,
    full_checkpoint=False,
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
        for name in ("docker", "sudo", "stat", "mv", "commit-boundary"):
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


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param("coordinator", id="entry=coordinator"),
        pytest.param("guard", id="entry=guard"),
    ],
)
@pytest.mark.parametrize(
    "output",
    [
        pytest.param("", id="output=empty-stdout"),
        pytest.param("private-unverified-command", id="output=private-command"),
        pytest.param("null", id="output=json-null"),
        pytest.param("[]", id="output=empty-array"),
        pytest.param("{}", id="output=json-object"),
        pytest.param('"findb-fetch-full-market"', id="output=json-string"),
        pytest.param("[null]", id="output=null-element"),
        pytest.param("[1]", id="output=number-element"),
        pytest.param('[""]', id="output=empty-command"),
        pytest.param('["unknown-command"]', id="output=unknown-command"),
        pytest.param('["findb-fetch-full-market",{}]', id="output=invalid-argument"),
        pytest.param('["findb-fetch-full-market"]', id="output=full-command"),
    ],
)
def test_cmd_query_unknown_role_rejects_before_any_effect_in_both_entries(entry, output):
    legacy = PROVIDERS[0][0]
    guard = (ROOT / "infra/deploy/check_full_market_checkpoint.sh").read_text()
    before = {
        name: row for name, row in original_rows().items() if not name.endswith("-historical")
    }
    before[legacy].update(cmd=["findb-fetch-full-market"], image="installed-full-image")
    completed, actual, pointer = execute(
        copy.deepcopy(before),
        target="production",
        mode="activate",
        source=guard if entry == "guard" else None,
        command_query=(legacy, output),
    )
    assert completed.returncode != 0
    reason = (
        "full_market_checkpoint_missing"
        if output == '["findb-fetch-full-market"]'
        else "command_unknown"
    )
    assert reason in completed.stderr
    assert "private-unverified-command" not in completed.stderr
    assert before == actual["rows"]
    assert pointer == "b" * 64 + "-122-1"
    assert not any(
        (
            call[0] == "mv"
            or (call[0] == "sudo" and "--consumer" in call)
            or call[:2]
            in [["docker", "stop"], ["docker", "start"], ["docker", "rename"], ["docker", "rm"]]
            for call in actual["log"]
        )
    )


@pytest.mark.parametrize(
    "entry,command",
    [
        pytest.param(
            "coordinator",
            ["findb-fetch-scheduler", "--note", "findb-fetch-full-market"],
            id="entry=coordinator-command=findb-fetch-scheduler---note-findb-fetch-full-market",
        ),
        pytest.param(
            "coordinator",
            [
                "findb-fetch-full-market",
                "--config",
                "/app/configs/full_market.production.v1.json",
            ],
            id="entry=coordinator-command=findb-fetch-full-market---config-/app/configs/full_market.production.v1.json",
        ),
        pytest.param(
            "guard",
            ["findb-fetch-scheduler", "--note", "findb-fetch-full-market"],
            id="entry=guard-command=findb-fetch-scheduler---note-findb-fetch-full-market",
        ),
        pytest.param(
            "guard",
            ["findb-fetch-finlab-scheduler", "--help"],
            id="entry=guard-command=findb-fetch-finlab-scheduler---help",
        ),
        pytest.param(
            "guard",
            ["findb-fetch-shioaji-scheduler", "--check"],
            id="entry=guard-command=findb-fetch-shioaji-scheduler---check",
        ),
        pytest.param(
            "guard",
            ["findb-fetch-taifex-pilot"],
            id="entry=guard-command=findb-fetch-taifex-pilot",
        ),
        pytest.param(
            "guard",
            [
                "findb-fetch-historical-backfill",
                "--provider",
                "twelve_data",
                "--run-forever",
            ],
            id="entry=guard-command=findb-fetch-historical-backfill---provider-twelve_data---run-forever",
        ),
        pytest.param(
            "guard",
            ["python", "-m", "findb_fetcher"],
            id="entry=guard-command=python--m-findb_fetcher",
        ),
        pytest.param(
            "guard",
            [
                "findb-fetch-full-market",
                "--config",
                "/app/configs/full_market.production.v1.json",
            ],
            id="entry=guard-command=findb-fetch-full-market---config-/app/configs/full_market.production.v1.json",
        ),
    ],
)
def test_known_cmd_roles_and_full_checkpoint_are_preserved_in_both_entries(entry, command):
    command = copy.deepcopy(command)
    legacy = PROVIDERS[0][0]
    guard = (ROOT / "infra/deploy/check_full_market_checkpoint.sh").read_text()
    before = {
        name: row for name, row in original_rows().items() if not name.endswith("-historical")
    }
    before[legacy]["cmd"] = command
    full = command[0] == "findb-fetch-full-market"
    completed, actual, _ = execute(
        copy.deepcopy(before),
        target="production",
        mode="activate",
        source=guard if entry == "guard" else None,
        full_checkpoint=full,
    )
    assert completed.returncode == 0, completed.stderr
    if full:
        assert actual["checkpoint_preserved"]
    if entry == "guard":
        assert before == actual["rows"]
    else:
        full_calls = [
            call
            for call in actual.get("provider_images", [])
            if call[0].startswith("findb-full-market-")
        ]
        assert bool(full_calls) == full


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param("coordinator", id="entry=coordinator"),
        pytest.param("guard", id="entry=guard"),
    ],
)
@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("corrupt", id="kind=corrupt"),
        pytest.param("unusable", id="kind=unusable"),
    ],
)
def test_valid_full_cmd_invalid_checkpoint_has_zero_effects_in_both_entries(entry, kind):
    legacy = PROVIDERS[0][0]
    guard = (ROOT / "infra/deploy/check_full_market_checkpoint.sh").read_text()
    before = {
        name: row for name, row in original_rows().items() if not name.endswith("-historical")
    }
    before[legacy]["cmd"] = ["findb-fetch-full-market"]
    completed, actual, pointer = execute(
        copy.deepcopy(before),
        target="production",
        mode="activate",
        source=guard if entry == "guard" else None,
        full_checkpoint=True,
        checkpoint_kind=kind,
    )
    assert completed.returncode != 0
    assert "full_market_checkpoint_invalid" in completed.stderr
    assert actual["checkpoint_preserved"]
    assert before == actual["rows"]
    assert pointer == "b" * 64 + "-122-1"
    assert not any(
        (
            call[0] == "mv"
            or (call[0] == "sudo" and "--consumer" in call)
            or call[:2]
            in [["docker", "stop"], ["docker", "start"], ["docker", "rename"], ["docker", "rm"]]
            for call in actual["log"]
        )
    )


@pytest.mark.parametrize(
    "running",
    [
        pytest.param(False, id="running=false"),
        pytest.param(True, id="running=true"),
    ],
)
@pytest.mark.parametrize(
    "name",
    [
        pytest.param("findb-full-market-twelve-data", id="name=findb-full-market-twelve-data"),
        pytest.param(
            "findb-full-market-twelve-data-previous",
            id="name=findb-full-market-twelve-data-previous",
        ),
    ],
)
@pytest.mark.parametrize(
    "rc",
    [
        pytest.param(1, id="rc=1"),
        pytest.param(81, id="rc=81"),
    ],
)
@pytest.mark.parametrize(
    "query",
    [
        pytest.param("list", id="query=list"),
        pytest.param("inspect", id="query=inspect"),
    ],
)
def test_whole_entry_unknown_inventory_precedes_every_external_effect(running, name, rc, query):
    before = original_rows()
    before[name] = dict(
        id="accepted-full",
        running=running,
        exit=0,
        oom=False,
        error="",
        accepted="true",
    )
    fault = f"inventory-list-{rc}" if query == "list" else f"inventory-inspect:{name}:{rc}"
    completed, state, _ = execute(copy.deepcopy(before), fault=fault)
    assert completed.returncode != 0
    assert "reason=query_unknown" in completed.stderr
    assert before == state["rows"]
    assert not any(
        (
            a[0] == "sudo"
            or (a[0] == "docker" and a[1] in ["stop", "start", "rm", "rename", "run", "create"])
            for a in state["log"]
        )
    )


@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param("", id="suffix=empty"),
        pytest.param("-previous", id="suffix=-previous"),
        pytest.param("-candidate", id="suffix=-candidate"),
        pytest.param("-transaction-backup", id="suffix=-transaction-backup"),
        pytest.param("-preflight-control", id="suffix=-preflight-control"),
    ],
)
@pytest.mark.parametrize(
    "running",
    [
        pytest.param(False, id="running=false"),
        pytest.param(True, id="running=true"),
    ],
)
@pytest.mark.parametrize(
    "rc",
    [
        pytest.param(1, id="rc=1"),
        pytest.param(81, id="rc=81"),
    ],
)
def test_initial_historical_and_interrupted_inventory_has_zero_effects(suffix, running, rc):
    before = original_rows()
    name = PROVIDERS[1][0] + "-historical" + suffix
    before[name] = dict(
        id="initial-touched",
        running=running,
        exit=0,
        oom=False,
        error="",
        accepted="true",
    )
    completed, state, pointer = execute(
        copy.deepcopy(before),
        mode="activate",
        fault=f"inventory-inspect:{name}:{rc}",
    )
    assert completed.returncode != 0
    assert "reason=query_unknown" in completed.stderr
    assert before == state["rows"]
    assert pointer == "b" * 64 + "-122-1"
    assert not any(
        (
            call[0] in ["sudo", "mv"]
            or (
                call[0] == "docker"
                and call[1] in ["stop", "start", "rm", "rename", "run", "create"]
            )
            for call in state["log"]
        )
    )


@pytest.mark.parametrize(
    "field,failure",
    [
        pytest.param(".Id", "rc1", id="field=.Id-failure=rc1"),
        pytest.param(".Id", "rc81", id="field=.Id-failure=rc81"),
        pytest.param(".Config.Image", "rc1", id="field=.Config.Image-failure=rc1"),
        pytest.param(".Config.Image", "rc81", id="field=.Config.Image-failure=rc81"),
        pytest.param(".State.Running", "rc1", id="field=.State.Running-failure=rc1"),
        pytest.param(".State.Running", "rc81", id="field=.State.Running-failure=rc81"),
        pytest.param(".State.ExitCode", "rc1", id="field=.State.ExitCode-failure=rc1"),
        pytest.param(".State.ExitCode", "rc81", id="field=.State.ExitCode-failure=rc81"),
        pytest.param(".State.OOMKilled", "rc1", id="field=.State.OOMKilled-failure=rc1"),
        pytest.param(".State.OOMKilled", "rc81", id="field=.State.OOMKilled-failure=rc81"),
        pytest.param(".State.Error", "rc1", id="field=.State.Error-failure=rc1"),
        pytest.param(".State.Error", "rc81", id="field=.State.Error-failure=rc81"),
    ],
)
def test_initial_historical_field_queries_reject_before_effects_query_failure(field, failure):
    name = PROVIDERS[1][0] + "-historical"
    before = original_rows()
    completed, state, pointer = execute(
        copy.deepcopy(before),
        mode="activate",
        fault=f"initial-field:{name}:{field}:{failure}",
    )
    assert completed.returncode != 0
    assert state["rows"] == before
    assert pointer == "b" * 64 + "-122-1"
    assert not any(
        (
            call[0] in ["sudo", "mv"]
            or (
                call[0] == "docker"
                and call[1] in ["stop", "start", "rm", "rename", "run", "create"]
            )
            for call in state["log"]
        )
    )


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({"id": ""}, id="changed=id=empty"),
        pytest.param({"image": ""}, id="changed=image=empty"),
        pytest.param({"running": "invalid"}, id="changed=running=invalid"),
        pytest.param({"exit": ""}, id="changed=exit=empty"),
        pytest.param({"oom": "invalid"}, id="changed=oom=invalid"),
    ],
)
def test_initial_historical_field_queries_reject_before_effects_malformed_value(
    changed,
):
    changed = copy.deepcopy(changed)
    name = PROVIDERS[1][0] + "-historical"
    before = original_rows()
    before[name].update(changed)
    completed, state, pointer = execute(copy.deepcopy(before), mode="activate")
    assert completed.returncode != 0
    assert state["rows"] == before
    assert pointer == "b" * 64 + "-122-1"
    assert not any(
        (
            call[0] in ["sudo", "mv"]
            or (
                call[0] == "docker"
                and call[1] in ["stop", "start", "rm", "rename", "run", "create"]
            )
            for call in state["log"]
        )
    )


@pytest.mark.parametrize(
    "running",
    [
        pytest.param(False, id="running=false"),
        pytest.param(True, id="running=true"),
    ],
)
@pytest.mark.parametrize(
    "fault",
    [
        pytest.param("", id="fault=empty"),
        pytest.param("provider_before", id="fault=provider_before"),
        pytest.param("provider_after", id="fault=provider_after"),
        pytest.param("provider_signal", id="fault=provider_signal"),
    ],
)
def test_previous_only_historical_retires_before_replacement_and_restores_on_failure(
    running, fault
):
    before = original_rows()
    for name, row in before.items():
        if name.endswith("-historical"):
            row.update(marker="date-boundary-v1", stop_exit=0)
    stable = PROVIDERS[0][0] + "-historical"
    previous = stable + "-previous"
    before[previous] = before.pop(stable)
    before[previous]["running"] = running
    completed, actual, pointer = execute(
        copy.deepcopy(before), target="production", mode="activate", fault=fault
    )
    if fault:
        assert completed.returncode != 0, completed.stderr
        assert_restored(before, actual)
        assert pointer == "b" * 64 + "-122-1"
        assert "transaction_rollback_failed" not in completed.stderr
    else:
        assert completed.returncode == 0, completed.stderr
        assert actual["rows"][stable]["running"]
        assert previous not in actual["rows"]
        creation = next(
            i for i, call in enumerate(actual["log"]) if call[0] == "sudo" and "--consumer" in call
        )
        if running:
            stop = actual["log"].index(["docker", "stop", "--time", "30", previous])
            assert stop < creation
        validations = [
            i
            for i, call in enumerate(actual["log"])
            if call[:2] == ["docker", "inspect"]
            and call[-1] == previous
            and "State.Running" in call[3]
        ]
        assert any((i < creation for i in validations))


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("stable", id="mode=stable"),
        pytest.param("previous-only", id="mode=previous-only"),
        pytest.param("interrupted-stable", id="mode=interrupted-stable"),
    ],
)
def test_bounded_replay_image_comes_from_registered_accepted_original(mode):
    before = original_rows()
    for name, row in before.items():
        if name.endswith("-historical"):
            row.update(marker="date-boundary-v1", stop_exit=0)
    stable = "findb-full-market-twelve-data"
    image = f"{REGISTRY}/findb/production/fetcher/twelve-data@sha256:{'b' * 64}"
    accepted = dict(
        id="accepted-full",
        image=image,
        running=False,
        exit=0,
        oom=False,
        error="",
        accepted="true",
        cmd="findb-fetch-full-market",
    )
    before[stable if mode == "stable" else stable + "-previous"] = accepted
    if mode == "interrupted-stable":
        before[stable] = dict(
            accepted,
            image=f"{REGISTRY}/findb/production/fetcher/twelve-data@sha256:{'d' * 64}",
            accepted="false",
            id="interrupted-full",
        )
    completed, actual, pointer = execute(
        copy.deepcopy(before),
        target="production",
        mode="activate",
        accepted_replay=True,
        full_checkpoint=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert [stable, image] in actual["provider_images"]
    assert [stable, before.get(stable, {}).get("image")] not in (
        actual["provider_images"] if mode == "interrupted-stable" else []
    )
    assert pointer == "a" * 64 + "-123-1"


@pytest.mark.parametrize(
    "command,replay,mode,running,fault",
    [
        pytest.param(
            "findb-fetch-scheduler",
            False,
            "candidate",
            True,
            "",
            id="command=findb-fetch-scheduler-replay=false-mode=candidate-running=true-fault=empty",
        ),
        pytest.param(
            "findb-fetch-scheduler",
            False,
            "activate",
            False,
            "",
            id="command=findb-fetch-scheduler-replay=false-mode=activate-running=false-fault=empty",
        ),
        pytest.param(
            "findb-fetch-scheduler",
            True,
            "candidate",
            True,
            "",
            id="command=findb-fetch-scheduler-replay=true-mode=candidate-running=true-fault=empty",
        ),
        pytest.param(
            "findb-fetch-scheduler",
            True,
            "activate",
            False,
            "",
            id="command=findb-fetch-scheduler-replay=true-mode=activate-running=false-fault=empty",
        ),
        pytest.param(
            "findb-fetch-full-market",
            False,
            "candidate",
            True,
            "",
            id="command=findb-fetch-full-market-replay=false-mode=candidate-running=true-fault=empty",
        ),
        pytest.param(
            "findb-fetch-full-market",
            False,
            "activate",
            False,
            "",
            id="command=findb-fetch-full-market-replay=false-mode=activate-running=false-fault=empty",
        ),
        pytest.param(
            "findb-fetch-full-market",
            True,
            "candidate",
            True,
            "",
            id="command=findb-fetch-full-market-replay=true-mode=candidate-running=true-fault=empty",
        ),
        pytest.param(
            "findb-fetch-full-market",
            True,
            "activate",
            False,
            "",
            id="command=findb-fetch-full-market-replay=true-mode=activate-running=false-fault=empty",
        ),
        pytest.param(
            None,
            False,
            "candidate",
            False,
            "",
            id="command=absent-replay=false-mode=candidate-running=false-fault=empty",
        ),
        pytest.param(
            "accepted-stable",
            True,
            "activate",
            True,
            "",
            id="command=accepted-stable-replay=true-mode=activate-running=true-fault=empty",
        ),
        pytest.param(
            "findb-fetch-scheduler",
            True,
            "activate",
            True,
            "provider_before",
            id="command=findb-fetch-scheduler-replay=true-mode=activate-running=true-fault=provider_before",
        ),
        pytest.param(
            "findb-fetch-scheduler",
            True,
            "activate",
            True,
            "provider_after",
            id="command=findb-fetch-scheduler-replay=true-mode=activate-running=true-fault=provider_after",
        ),
        pytest.param(
            "findb-fetch-scheduler",
            True,
            "activate",
            True,
            "provider_signal",
            id="command=findb-fetch-scheduler-replay=true-mode=activate-running=true-fault=provider_signal",
        ),
        pytest.param(
            "findb-fetch-scheduler",
            True,
            "activate",
            True,
            "stop-refused",
            id="command=findb-fetch-scheduler-replay=true-mode=activate-running=true-fault=stop-refused",
        ),
        pytest.param(
            "findb-fetch-scheduler",
            True,
            "activate",
            True,
            "query-unknown",
            id="command=findb-fetch-scheduler-replay=true-mode=activate-running=true-fault=query-unknown",
        ),
    ],
)
def test_legacy_full_migration_selects_accepted_original_before_command(
    command, replay, mode, running, fault
):
    pilot = PROVIDERS[0][0]
    full = "findb-full-market-twelve-data"
    image = "installed-full-image"
    before = {
        name: row for name, row in original_rows().items() if not name.endswith("-historical")
    }
    for row in before.values():
        row["running"] = False
    original_name = pilot if command == "accepted-stable" else pilot + "-previous"
    accepted = dict(
        id="accepted-legacy-full",
        image=image,
        running=running,
        exit=0,
        oom=False,
        error="",
        accepted="true",
        cmd="findb-fetch-full-market",
    )
    before.pop(pilot)
    before[original_name] = accepted
    expected = copy.deepcopy(before)
    if command not in [None, "accepted-stable"]:
        before[pilot] = dict(
            accepted,
            id="interrupted-stable",
            image="interrupted-image",
            accepted="false",
            cmd=command,
            running=False,
        )
    if fault == "stop-refused":
        accepted["stop_error"] = True
        expected[original_name]["stop_error"] = True
    injected = (
        f"initial-field:{original_name}:.Config.Cmd:rc81"
        if fault == "query-unknown"
        else ("" if fault == "stop-refused" else fault)
    )
    completed, actual, pointer = execute(
        copy.deepcopy(before),
        target="production",
        mode=mode,
        full_checkpoint=True,
        accepted_replay=replay,
        fault=injected,
    )
    assert actual["checkpoint_preserved"]
    assert not any((call[:2] == ["docker", "rm"] and "-f" in call for call in actual["log"]))
    assert "interrupted-stable" not in actual.get("started_ids", [])
    if fault or mode == "candidate":
        if fault:
            assert completed.returncode != 0
        else:
            assert completed.returncode == 0, completed.stderr
        assert_restored(expected, actual)
        assert actual["rows"][original_name]["image"] == image
        assert "transaction_rollback_failed" not in completed.stderr
        assert pointer == "b" * 64 + "-122-1"
        assert not any(
            (
                call[:2] == ["docker", "rm"] and call[-1] in [original_name, "accepted-legacy-full"]
                for call in actual["log"]
            )
        )
    else:
        assert completed.returncode == 0, completed.stderr
        assert "fetcher_aws_deploy=activated" in completed.stdout
        assert actual["rows"][full]["running"]
        assert pointer == "a" * 64 + "-123-1"
    if fault == "query-unknown":
        assert before == actual["rows"]
        assert not any(
            (
                call[0] == "mv"
                or (call[0] == "sudo" and "--consumer" in call)
                or call[:2]
                in [["docker", "stop"], ["docker", "rename"], ["docker", "rm"], ["docker", "start"]]
                for call in actual["log"]
            )
        )
    elif fault != "stop-refused":
        full_calls = [call for call in actual.get("provider_images", []) if call[0] == full]
        assert full_calls
        expected_image = (
            image
            if replay
            else f"289112218471.dkr.ecr.ap-southeast-1.amazonaws.com/findb/production/fetcher/twelve-data@sha256:{'c' * 64}"
        )
        assert full_calls[0][1] == expected_image


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param(".State.ExitCode", "137", id="field=.State.ExitCode-value=137"),
        pytest.param(".State.OOMKilled", "true", id="field=.State.OOMKilled-value=true"),
        pytest.param(
            ".State.Error",
            "private-host-error",
            id="field=.State.Error-value=private-host-error",
        ),
        pytest.param(".Id", "rc1", id="field=.Id-value=rc1"),
        pytest.param(".Id", "rc81", id="field=.Id-value=rc81"),
        pytest.param(".Id", "invalid", id="field=.Id-value=invalid"),
        pytest.param(".Id", "empty", id="field=.Id-value=empty"),
        pytest.param(".Config.Image", "rc1", id="field=.Config.Image-value=rc1"),
        pytest.param(".Config.Image", "rc81", id="field=.Config.Image-value=rc81"),
        pytest.param(".Config.Image", "invalid", id="field=.Config.Image-value=invalid"),
        pytest.param(".Config.Image", "empty", id="field=.Config.Image-value=empty"),
        pytest.param(".State.Running", "rc1", id="field=.State.Running-value=rc1"),
        pytest.param(".State.Running", "rc81", id="field=.State.Running-value=rc81"),
        pytest.param(".State.Running", "invalid", id="field=.State.Running-value=invalid"),
        pytest.param(".State.Running", "empty", id="field=.State.Running-value=empty"),
        pytest.param(".State.ExitCode", "rc1", id="field=.State.ExitCode-value=rc1"),
        pytest.param(".State.ExitCode", "rc81", id="field=.State.ExitCode-value=rc81"),
        pytest.param(".State.ExitCode", "invalid", id="field=.State.ExitCode-value=invalid"),
        pytest.param(".State.ExitCode", "empty", id="field=.State.ExitCode-value=empty"),
        pytest.param(".State.OOMKilled", "rc1", id="field=.State.OOMKilled-value=rc1"),
        pytest.param(".State.OOMKilled", "rc81", id="field=.State.OOMKilled-value=rc81"),
        pytest.param(".State.OOMKilled", "invalid", id="field=.State.OOMKilled-value=invalid"),
        pytest.param(".State.OOMKilled", "empty", id="field=.State.OOMKilled-value=empty"),
        pytest.param(".State.Error", "rc1", id="field=.State.Error-value=rc1"),
        pytest.param(".State.Error", "rc81", id="field=.State.Error-value=rc81"),
        pytest.param(".State.Error", "invalid", id="field=.State.Error-value=invalid"),
    ],
)
def test_postcommit_cleanup_failure_preserves_committed_replacement_and_old_ids(field, value):
    before = original_rows()
    for name, row in before.items():
        if name.endswith("-historical"):
            row.update(marker="date-boundary-v1", stop_exit=0)
    completed, actual, pointer = execute(
        copy.deepcopy(before),
        target="production",
        mode="activate",
        postcommit_field=(field, "" if value == "empty" else value),
    )
    assert completed.returncode != 0
    assert "committed_cleanup_failed committed=true" in completed.stderr
    assert "transaction_aborted" not in completed.stderr
    assert "fetcher_aws_deploy=activated" not in completed.stdout
    assert "private-host-error" not in completed.stderr
    assert pointer == "a" * 64 + "-123-1"
    assert {row["id"] for row in before.values()} <= {row["id"] for row in actual["rows"].values()}
    assert actual["rows"][PROVIDERS[0][0]]["running"]
    assert not any((call[:2] == ["docker", "rm"] for call in actual["log"]))


@pytest.mark.parametrize(
    "field,rc",
    [
        pytest.param(".Id", 1, id="field=.Id-rc=1"),
        pytest.param(".Id", 81, id="field=.Id-rc=81"),
        pytest.param(".Config.Image", 1, id="field=.Config.Image-rc=1"),
        pytest.param(".Config.Image", 81, id="field=.Config.Image-rc=81"),
    ],
)
def test_postcommit_matching_identity_stdout_nonzero_preserves_old_ids(field, rc):
    before = original_rows()
    for name, row in before.items():
        if name.endswith("-historical"):
            row.update(marker="date-boundary-v1", stop_exit=0)
    completed, actual, pointer = execute(
        copy.deepcopy(before),
        target="production",
        mode="activate",
        full_checkpoint=True,
        postcommit_field=(field, f"match-rc{rc}"),
    )
    assert completed.returncode != 0
    assert "committed_cleanup_failed committed=true" in completed.stderr
    assert "fetcher_aws_deploy=activated" not in completed.stdout
    assert "transaction_aborted" not in completed.stderr
    assert actual["checkpoint_preserved"]
    assert actual["rows"][PROVIDERS[0][0]]["running"]
    assert pointer == "a" * 64 + "-123-1"
    assert {row["id"] for row in before.values()} <= {row["id"] for row in actual["rows"].values()}
    assert not any((call[:2] == ["docker", "rm"] for call in actual["log"]))


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("invalid-label", id="failure=invalid-label"),
        pytest.param("rc1", id="failure=rc1"),
        pytest.param("rc81", id="failure=rc81"),
    ],
)
def test_replay_unknown_accepted_metadata_rejects_before_runtime_effects(failure):
    before = original_rows()
    stable = "findb-full-market-twelve-data"
    before[stable + "-previous"] = dict(
        id="accepted-full",
        image="accepted-image",
        running=False,
        exit=0,
        oom=False,
        error="",
        accepted="unknown" if failure == "invalid-label" else "true",
        cmd="findb-fetch-full-market",
    )
    fault = (
        ""
        if failure == "invalid-label"
        else f"initial-field:{stable}-previous:.Config.Labels:{failure}"
    )
    completed, actual, pointer = execute(
        copy.deepcopy(before),
        target="production",
        mode="activate",
        accepted_replay=True,
        full_checkpoint=True,
        fault=fault,
    )
    assert completed.returncode != 0
    assert before == actual["rows"]
    assert pointer == "b" * 64 + "-122-1"
    assert not any(
        (
            call[0] == "sudo"
            and "--consumer" in call
            or call[:2]
            in [["docker", "stop"], ["docker", "rename"], ["docker", "rm"], ["docker", "start"]]
            for call in actual["log"]
        )
    )


@pytest.mark.parametrize(
    "running",
    [
        pytest.param(False, id="running=false"),
        pytest.param(True, id="running=true"),
    ],
)
@pytest.mark.parametrize(
    "fault",
    [
        pytest.param("", id="fault=empty"),
        pytest.param("provider_after", id="fault=provider_after"),
    ],
)
def test_historical_interrupted_stable_preserves_selected_previous(running, fault):
    before = original_rows()
    for name, row in before.items():
        if name.endswith("-historical"):
            row.update(marker="date-boundary-v1", stop_exit=0)
    stable = PROVIDERS[0][0] + "-historical"
    previous = stable + "-previous"
    before[previous] = before.pop(stable)
    before[previous]["running"] = running
    expected = copy.deepcopy(before)
    before[stable] = dict(
        before[previous],
        id="interrupted-historical",
        accepted="false",
        running=True,
    )
    completed, actual, _ = execute(
        copy.deepcopy(before), target="production", mode="activate", fault=fault
    )
    if fault:
        assert completed.returncode != 0
        assert_restored(expected, actual)
        assert "transaction_rollback_failed" not in completed.stderr
    else:
        assert completed.returncode == 0, completed.stderr
        assert actual["rows"][stable]["id"] == "new-" + stable
        assert previous not in actual["rows"]
    assert "interrupted-historical" not in actual.get("started_ids", [])


def test_accepted_legacy_historical_137_passes_postcommit_cleanup():
    completed, actual, pointer = execute(original_rows(), mode="activate")
    assert completed.returncode == 0, completed.stderr
    assert "fetcher_aws_deploy=activated" in completed.stdout
    assert pointer == "a" * 64 + "-123-1"
    assert not any((row["id"].startswith("old-") for row in actual["rows"].values()))


def assert_restored(before, after):
    for name, original in before.items():
        assert after["rows"][name]["id"] == original["id"]
        assert after["rows"][name]["running"] == original["running"], name


def test_legacy_retirement_and_candidate_restores_originals():
    before = original_rows()
    completed, state, pointer = execute(before)
    assert completed.returncode == 0, completed.stderr
    assert_restored(before, state)
    assert pointer == "b" * 64 + "-122-1"
    assert completed.stderr.count("legacy_historical_crash_recovery") == 3


def test_retry_handles_previously_stopped_legacy_137():
    before = original_rows()
    before[PROVIDERS[0][0] + "-historical"].update(running=False, exit=137)
    completed, state, _ = execute(before)
    assert completed.returncode == 0, completed.stderr
    assert_restored(before, state)


@pytest.mark.parametrize(
    "profile",
    [
        pytest.param("bounded", id="profile=bounded"),
        pytest.param("full-market", id="profile=full-market"),
    ],
)
def test_legacy_exception_does_not_apply_to_production_or_full_market(profile):
    before = original_rows()
    completed, state, _ = execute(before, target="production", profile=profile)
    assert completed.returncode != 0
    assert "runtime_retirement_failed" in completed.stderr
    assert_restored(before, state)


def test_absent_historical_is_normal():
    before = original_rows()
    del before[PROVIDERS[0][0] + "-historical"]
    completed, state, _ = execute(before)
    assert completed.returncode == 0, completed.stderr
    assert_restored(before, state)


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("candidate", id="mode=candidate"),
        pytest.param("activate", id="mode=activate"),
    ],
)
@pytest.mark.parametrize(
    "fault",
    [
        pytest.param("provider_before", id="fault=provider_before"),
        pytest.param("provider_after", id="fault=provider_after"),
        pytest.param("rename_once", id="fault=rename_once"),
    ],
)
def test_failed_provider_transactions_restore_containers_and_pointer(mode, fault):
    before = original_rows()
    completed, state, pointer = execute(before, mode=mode, fault=fault)
    assert completed.returncode != 0
    assert "transaction_aborted" in completed.stderr
    assert_restored(before, state)
    assert pointer == "b" * 64 + "-122-1"


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"oom": True}, id="change=oom=true"),
        pytest.param({"error": "sensitive-host-error"}, id="change=error=sensitive-host-error"),
        pytest.param({"stop_exit": 1}, id="change=stop_exit=1"),
        pytest.param({"marker": "date-boundary-v1"}, id="change=marker=date-boundary-v1"),
        pytest.param(
            {
                "image": "unknown@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
            },
            id="change=image=unknown@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
        ),
        pytest.param(
            {"cmd": "findb-fetch-scheduler --run-forever"},
            id="change=cmd=findb-fetch-scheduler --run-forever",
        ),
        pytest.param({"accepted": "false"}, id="change=accepted=false"),
        pytest.param({"path": "unexpected-entrypoint"}, id="change=path=unexpected-entrypoint"),
        pytest.param(
            {"args": ["--provider", "twelve_data", "--run-forever", "--extra"]},
            id="change=args=--provider-twelve_data---run-forever---extra",
        ),
        pytest.param(
            {"cmd": "findb-fetch-historical-backfill --provider finlab --run-forever"},
            id="change=cmd=findb-fetch-historical-backfill --provider finlab --run-forever",
        ),
        pytest.param({"stop_error": True}, id="change=stop_error=true"),
    ],
)
def test_nonlegacy_or_unsafe_retirement_aborts_and_recovers(change):
    change = copy.deepcopy(change)
    before = original_rows()
    before[PROVIDERS[0][0] + "-historical"].update(change)
    completed, state, _ = execute(before)
    assert completed.returncode != 0
    assert "transaction_aborted" in completed.stderr
    assert "sensitive-host-error" not in completed.stderr
    if change.get("accepted") != "false":
        assert_restored(before, state)


def test_new_historical_clean_exit_activates():
    before = original_rows()
    for name, row in before.items():
        if name.endswith("-historical"):
            row.update(marker="date-boundary-v1", stop_exit=0)
    completed, state, pointer = execute(before, mode="activate")
    assert completed.returncode == 0, completed.stderr
    assert pointer == "a" * 64 + "-123-1"
    assert not any((name.endswith("-previous") for name in state["rows"]))
    assert all((row["id"].startswith("new-") for row in state["rows"].values()))
    assert set(state["rows"]) == {row[0] for row in PROVIDERS} | {"findb-fetcher-taifex-scheduler"}
    assert not any((name.endswith("-historical") for name in state["rows"]))


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({"oom": True}, id="changed=oom=true"),
        pytest.param(
            {"error": "private-original-error"},
            id="changed=error=private-original-error",
        ),
    ],
)
def test_commit_preparation_rejects_unsafe_original_before_removal(changed):
    changed = copy.deepcopy(changed)
    before = original_rows()
    for name, row in before.items():
        if name.endswith("-historical"):
            row.update(marker="date-boundary-v1", stop_exit=0)
    before[PROVIDERS[0][0]].update(changed)
    completed, state, pointer = execute(copy.deepcopy(before), mode="activate")
    assert completed.returncode != 0
    assert "transaction_aborted" in completed.stderr
    assert "private-original-error" not in completed.stderr
    assert_restored(before, state)
    assert pointer == "b" * 64 + "-122-1"
    original_ids = {row["id"] for row in before.values()}
    removals = [call[-1] for call in state["log"] if call[:2] == ["docker", "rm"]]
    assert original_ids.isdisjoint(removals)


def test_ordinary_scheduler_137_remains_failure():
    before = original_rows()
    before[PROVIDERS[0][0]]["stop_exit"] = 137
    completed, state, pointer = execute(before, mode="activate")
    assert completed.returncode != 0
    assert_restored(before, state)
    assert pointer == "b" * 64 + "-122-1"


def test_retirement_failure_does_not_restore_stale_previous():
    before = original_rows()
    historical = PROVIDERS[0][0] + "-historical"
    before[historical]["stop_exit"] = 1
    before[historical + "-previous"] = copy.deepcopy(before[historical])
    before[historical + "-previous"].update(id="stale", running=False)
    completed, state, _ = execute(before)
    assert completed.returncode != 0
    assert state["rows"][historical]["id"] == before[historical]["id"]
    assert state["rows"][historical]["running"]


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("candidate", id="mode=candidate"),
        pytest.param("activate", id="mode=activate"),
    ],
)
@pytest.mark.parametrize(
    "boundary",
    [
        pytest.param("before", id="boundary=before"),
        pytest.param("after", id="boundary=after"),
    ],
)
@pytest.mark.parametrize(
    "signum,code",
    [
        pytest.param("INT", 130, id="signum=INT-code=130"),
        pytest.param("TERM", 143, id="signum=TERM-code=143"),
        pytest.param("HUP", 129, id="signum=HUP-code=129"),
    ],
)
def test_signals_before_and_after_registration_restore_prior_work_and_pointer(
    mode, boundary, signum, code
):
    source = DEPLOY.read_text()
    registration = '  processed+=("${stable}:${previous}:${old_available}:${original_id}")'
    start = source.index("register_named_full() {")
    end = source.index("\n}\n", start) + len("\n}")
    function = source[start:end]
    assert function.count(registration) == 1
    second = PROVIDERS[1][0] + "-historical"
    injection = f'  if [ "$stable" = {second} ]; then kill -{signum} "$$"; fi'
    replacement = (
        injection + "\n" + registration if boundary == "before" else registration + "\n" + injection
    )
    before = original_rows()
    completed, state, pointer = execute(
        before,
        mode=mode,
        source=source[:start] + function.replace(registration, replacement) + source[end:],
    )
    assert completed.returncode == code, completed.stderr
    assert "transaction_aborted" in completed.stderr
    assert "rollback_failed" not in completed.stderr
    assert_restored(before, state)
    assert pointer == "b" * 64 + "-122-1"
    assert not any((name.endswith("-previous") for name in state["rows"]))
    # First retirement occurred; the current registration
    # boundary precedes any mutation of the second runtime.
    renames = [call for call in state["log"] if call[:2] == ["docker", "rename"]]
    assert PROVIDERS[0][0] + "-historical" in renames[0]
    assert not any((second in call for call in renames))


@pytest.mark.parametrize(
    "retained",
    [
        pytest.param(
            "findb-fetcher-scheduler-historical",
            id="retained=findb-fetcher-scheduler-historical",
        ),
        pytest.param(
            "findb-fetcher-taifex-scheduler",
            id="retained=findb-fetcher-taifex-scheduler",
        ),
    ],
)
@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("candidate", id="mode=candidate"),
        pytest.param("activate", id="mode=activate"),
    ],
)
def test_unaccepted_stable_never_displaces_accepted_retained_original(retained, mode):
    before = original_rows()
    original = copy.deepcopy(before.get(retained, before[PROVIDERS[0][0]]))
    command = (
        [
            "findb-fetch-historical-backfill",
            "--provider",
            "twelve_data",
            "--run-forever",
        ]
        if retained.endswith("-historical")
        else [
            "findb-fetch-taifex-pilot",
            "--config",
            "/app/configs/taifex_tw_staging_pilot.v1.json",
        ]
    )
    original.update(id="accepted-original", running=False, exit=0, stop_exit=0, cmd=command)
    before[retained + "-previous"] = original
    before[retained] = dict(
        id="unaccepted-stable",
        running=True,
        exit=0,
        stop_exit=0,
        oom=False,
        error="",
        accepted="false",
        marker="date-boundary-v1",
        cmd=command,
    )
    completed, state, pointer = execute(before, mode=mode, fault="provider_before")
    assert completed.returncode != 0
    assert state["rows"][retained + "-previous"]["id"] == original["id"]
    assert not state["rows"][retained + "-previous"]["running"]
    assert retained not in state["rows"]
    assert pointer == "b" * 64 + "-122-1"
    operations = state["log"]
    assert ["docker", "rm", retained + "-previous"] not in operations
    assert ["docker", "rename", retained, retained + "-previous"] not in operations
    assert "unaccepted-stable" not in state.get("started_ids", [])


@pytest.mark.parametrize(
    "retained",
    [
        pytest.param(
            "findb-fetcher-scheduler-historical",
            id="retained=findb-fetcher-scheduler-historical",
        ),
        pytest.param(
            "findb-fetcher-taifex-scheduler",
            id="retained=findb-fetcher-taifex-scheduler",
        ),
    ],
)
@pytest.mark.parametrize(
    "mode",
    [
        pytest.param("candidate", id="mode=candidate"),
        pytest.param("activate", id="mode=activate"),
    ],
)
def test_unaccepted_retained_without_stable_fails_closed_without_starting_it(retained, mode):
    before = original_rows()
    before.pop(retained, None)
    false_previous = dict(
        id="unaccepted-previous",
        running=False,
        exit=0,
        oom=False,
        error="",
        accepted="false",
        cmd=[
            "findb-fetch-historical-backfill",
            "--provider",
            "twelve_data",
            "--run-forever",
        ]
        if retained.endswith("-historical")
        else [
            "findb-fetch-taifex-pilot",
            "--config",
            "/app/configs/taifex_tw_staging_pilot.v1.json",
        ],
    )
    before[retained + "-previous"] = false_previous
    completed, state, pointer = execute(before, mode=mode)
    assert completed.returncode != 0
    assert "unaccepted_retained_runtime" in completed.stderr
    assert retained not in state["rows"]
    assert state["rows"][retained + "-previous"] == false_previous
    assert pointer == "b" * 64 + "-122-1"
    starts = [call[-1] for call in state["log"] if call[:2] == ["docker", "start"]]
    assert retained not in starts
    assert retained + "-previous" not in starts
    assert "unaccepted-previous" not in state.get("started_ids", [])


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"stop_exit": 137}, id="change=stop_exit=137"),
        pytest.param({"oom": True}, id="change=oom=true"),
        pytest.param({"error": "sensitive-error"}, id="change=error=sensitive-error"),
    ],
)
def test_unaccepted_stable_strict_stop_failure_preserves_accepted_previous(change):
    change = copy.deepcopy(change)
    retained = PROVIDERS[0][0] + "-historical"
    before = original_rows()
    original = copy.deepcopy(before[retained])
    original.update(id="accepted-original", running=False, exit=0, stop_exit=0)
    before[retained + "-previous"] = original
    before[retained].update(accepted="false", stop_exit=0, marker="date-boundary-v1")
    before[retained].update(change)
    completed, state, pointer = execute(before, mode="activate")
    assert completed.returncode != 0
    assert "runtime_retirement_failed" in completed.stderr
    assert state["rows"][retained + "-previous"]["id"] == original["id"]
    assert pointer == "b" * 64 + "-122-1"
    assert "sensitive-error" not in completed.stderr
    starts = [call[-1] for call in state["log"] if call[:2] == ["docker", "start"]]
    assert retained not in starts
