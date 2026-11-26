"""Execute the deployment coordinator against a stateful fake Docker daemon.

Only /opt/fetcher paths are relocated into a temporary directory. The actual
validation, ERR trap, retirement, candidate and rollback shell paths execute.
"""

import hashlib
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

import fake_daemon
import pytest
from script_env import script_env

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh"
REGISTRY = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
# The installed consumers come from the generated inventory, as in the deploy script.
INVENTORY = json.loads((ROOT / "infra/deploy/generated/provider_inventory.v1.json").read_text())
PROVIDERS = tuple(
    (
        consumer["name"],
        consumer["image_key"].replace("_", "-"),
        consumer["provider"],
        consumer["role"],
    )
    for role in ("full_market", "historical")
    for consumer in INVENTORY["consumers"]
    if consumer["role"] == role
)
# How a consumer started by the transaction can be found ended during activation.
NEW_RUNTIME_ENDINGS = {
    "new-exit-2": {"exit": 2},
    "new-exit-1": {"exit": 1},
    "new-exit-137": {"exit": 137},
    "new-oom": {"exit": 137, "oom": True},
    "new-error": {"exit": 1, "error": "private-error"},
}


def fake_call(state, command, args, parent, environ):
    """Apply one fake command to the shared state; return (status, stdout)."""
    rows = state["rows"]
    state["log"].append([command, *args])
    output = []
    status = 0
    if command == "commit-boundary":
        state["committed"] = True
    elif command == "sleep":
        pass
    elif command == "stat":
        output.append("root:root:700")
    elif command == "mv":
        os.replace(args[-2], args[-1])
    elif command == "sudo":
        # Runtime secret wrapper itself is not under test. No credentials are used.
        if args[0] == "test":
            path = Path(args[-1])
            probe = {
                "-f": path.is_file,
                "-d": path.is_dir,
                "-e": path.exists,
                "-L": path.is_symlink,
            }[args[1]]
            return (0 if probe() else 1), ""
        if args[0] == "python3":
            return "local", ""
        if args[args.index("--consumer") + 1] == "images":
            state["images_prepared"] = True
            state["prepared_images"] = sorted(a for a in args if "@sha256:" in a)
            return 0, ""
        index = next(i for i, a in enumerate(args) if a.endswith("/release_fetcher_provider.sh"))
        provider, image, state_dir, state_path, stable, candidate, previous = args[
            index + 1 : index + 8
        ]
        state.setdefault("provider_images", []).append([stable, image])
        state.setdefault("provider_layouts", {})[stable] = [image, state_dir, args[index + 9]]
        runtime_command = args[index + 10 :]
        if state.get("fault") == "provider_before":
            status = 7
        else:
            row = rows.get(stable)
            if row and row.get("accepted") == "false":
                rows.pop(stable)
                row = None
            if row is None and previous in rows:
                rows[stable] = rows.pop(previous)
                row = rows[stable]
                row["running"] = True
            if row:
                row["running"] = False
                row["exit"] = row.get("stop_exit", 0)
                if row["exit"] != 0:
                    status = 8
                elif state.get("fault") == "rename_once":
                    state["fault"] = "used"
                    status = 5
                else:
                    rows[previous] = rows.pop(stable)
            if not status and environ["FETCHER_DEPLOY_MODE"] == "activate":
                rows[stable] = dict(
                    id="new-" + stable,
                    image=image,
                    running=True,
                    exit=0,
                    oom=False,
                    error="",
                    accepted="true",
                    cmd=runtime_command,
                )
                if (
                    environ["APP_ENVIRONMENT"] == "production"
                    and environ.get("FETCHER_RUNTIME_PROFILE", "bounded") == "bounded"
                ):
                    rows[stable + "-historical"] = dict(
                        id="new-" + stable + "-historical",
                        running=True,
                        exit=0,
                        oom=False,
                        error="",
                        accepted="true",
                        marker="date-boundary-v1",
                        cmd=[
                            "findb-fetch-historical-backfill",
                            "--provider",
                            provider.replace("-", "_"),
                            "--run-forever",
                        ],
                    )
            fault = state.get("fault", "")
            if (
                not status
                and fault.startswith("new-")
                and environ["FETCHER_DEPLOY_MODE"] == "activate"
            ):
                # The new consumer ends, or will not stop cleanly, after it was started.
                if stable == PROVIDERS[0][0]:
                    rows[stable].update(running=False, **NEW_RUNTIME_ENDINGS[fault.split("+")[0]])
                elif stable == PROVIDERS[1][0] and fault.endswith("+unclean-stop"):
                    rows[stable]["stop_exit"] = 137
            if not status and state.get("fault") == "provider_after":
                status = 9
            if not status and state.get("fault") == "provider_signal":
                # Delivered while the caller still waits on this command.
                os.kill(parent, signal.SIGTERM)
    elif command == "docker":
        if args[:2] == ["image", "prune"]:
            pass
        elif args[:2] == ["container", "ls"]:
            if state.get("fault", "").startswith("inventory-list-"):
                status = int(state["fault"].rsplit("-", 1)[1])
            else:
                output.append("\n".join(rows))
        elif args[0] == "container":
            if (
                state.get("fault", "").startswith("inventory-inspect:")
                and args[-1] == state["fault"].split(":")[1]
            ):
                status = int(state["fault"].split(":")[2])
            else:
                status = 0 if args[-1] in rows else 1
        elif args[0] == "inspect":
            row = rows.get(args[-1])
            if row is None:
                row = next((row for row in rows.values() if row["id"] == args[-1]), None)
            if row is None:
                status = 1
            else:
                template = args[2]
                field_fault = state.get("postcommit_field") if state.get("committed") else None
                if field_fault and args[-1].startswith("old-") and field_fault[0] in template:
                    if field_fault[1] in ["match-rc1", "match-rc81"]:
                        output.append(
                            row["id"] if field_fault[0] == ".Id" else row.get("image", "new")
                        )
                        status = int(field_fault[1].rsplit("rc", 1)[1])
                    elif field_fault[1] in ["rc1", "rc81"]:
                        status = int(field_fault[1][2:])
                    else:
                        output.append(field_fault[1])
                elif ".State.Running" in template:
                    output.append(str(row["running"]).lower())
                elif ".State.Restarting" in template:
                    output.append(str(row.get("restarting", False)).lower())
                elif ".RestartCount" in template:
                    output.append("1" if state.get("fault") == "activation-restarts" else "0")
                elif ".State.ExitCode" in template:
                    output.append(str(row["exit"]))
                elif ".State.OOMKilled" in template:
                    output.append(str(row["oom"]).lower())
                elif ".State.Error" in template:
                    output.append(row["error"])
                elif ".Id" in template:
                    output.append(row["id"])
                elif ".Config.Image" in template:
                    output.append(row.get("image", "new"))
                elif (
                    template == "{{json .Config.Cmd}}"
                    and state.get("command_query")
                    and args[-1] == state["command_query"][0]
                ):
                    output.append(state["command_query"][1])
                elif ".Config.Cmd" in template:
                    cmd = (
                        row["cmd"]
                        if isinstance(row.get("cmd"), list)
                        else row.get("cmd", "").split()
                    )
                    output.append(
                        json.dumps(cmd, separators=(",", ":"))
                        if template == "{{json .Config.Cmd}}"
                        else " ".join(cmd)
                    )
                elif ".Path" in template:
                    output.append(row.get("path", "findb-fetch-historical-backfill"))
                elif ".Args" in template:
                    output.append(
                        json.dumps(
                            row.get("args", row.get("cmd", "").split()[1:]), separators=(",", ":")
                        )
                    )
                elif "historical-shutdown" in template:
                    output.append("present" if "marker" in row else "")
                elif "accepted" in template:
                    output.append(row["accepted"])
                else:
                    status = 99
                fault = state.get("fault", "").split(":")
                if (
                    len(fault) == 4
                    and fault[0] == "initial-field"
                    and args[-1] == fault[1]
                    and fault[2] in template
                ):
                    status = int(fault[3][2:])
        elif args[0] == "exec":
            if rows[args[1]].get("control") == "running":
                output.append(
                    "fetcher_deployment_blocked reason=scheduler_not_stopped "
                    "execution_profile=full_market provider=shioaji"
                )
                status = 2
        elif args[0] == "stop":
            row = rows[args[-1]]
            if row.get("stop_error"):
                status = 6
            else:
                row["running"] = False
                row["exit"] = row.get("stop_exit", 0)
        elif args[0] == "start":
            state.setdefault("started_ids", []).append(rows[args[-1]]["id"])
            rows[args[-1]]["running"] = True
        elif args[0] == "rm":
            name = args[-1]
            if name not in rows:
                name = next(n for n, r in rows.items() if r["id"] == name)
            if rows[name]["running"]:
                status = 81
            else:
                rows.pop(name)
        elif args[0] == "rename":
            if state.get("fault") == "rename_once":
                state["fault"] = "used"
                status = 5
            else:
                rows[args[2]] = rows.pop(args[1])
        else:
            status = 99
    else:
        status = 99
    return status, "".join(line + "\n" for line in output)


# Placeholder for the Full checkpoint directory; execute() binds it to the harness root.
FULL_STATE = "@full-state@"


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
                else f"findb-fetch-runtime --execution-profile {profile} "
                f"--state-path {FULL_STATE}/state-{provider}.sqlite3 --provider {provider} --run-forever"
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
    inventory_change=None,
):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        host = root / "fetcher"
        release = host / "releases" / ("a" * 64 + "-123-1")
        old_release = host / "releases" / ("b" * 64 + "-122-1")
        old_release.mkdir(parents=True)
        runtime = release / "infra/deploy/runtime-secrets"
        runtime.mkdir(parents=True)
        inventory = release / "infra/deploy/generated/provider_inventory.v1.json"
        inventory.parent.mkdir(parents=True)
        inventory.write_bytes(
            (ROOT / "infra/deploy/generated/provider_inventory.v1.json").read_bytes()
        )
        if inventory_change is not None:
            value = json.loads(inventory.read_text())
            value.pop("projection_sha256")
            inventory_change(value)
            value["projection_sha256"] = hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            inventory.write_text(json.dumps(value))
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
            # Each Full consumer runs on its own checkpoint (#691).
            files = [
                state_dir / f"state-{provider}.sqlite3"
                for _, _, provider, profile in PROVIDERS
                if profile == "full_market"
            ]
            for path in files:
                with sqlite3.connect(path) as db:
                    db.executescript(
                        (ROOT / "fetcher/tests/fixtures/full_market_legacy_core.sql").read_text()
                    )
                if checkpoint_kind == "corrupt":
                    with sqlite3.connect(path) as db:
                        page = db.execute(
                            "SELECT rootpage FROM sqlite_master WHERE name='full_work'"
                        ).fetchone()[0]
                        size = db.execute("PRAGMA page_size").fetchone()[0]
                    with path.open("r+b") as raw:
                        raw.seek((page - 1) * size)
                        raw.write(b"\0")
                elif checkpoint_kind == "unusable":
                    with sqlite3.connect(path) as db:
                        db.executescript(
                            "DROP TABLE full_work; CREATE TABLE full_work(unusable TEXT);"
                        )
            script_source = script_source.replace("/var/lib/findb-full-market", str(checkpoint))
            for row in rows.values():
                if isinstance(row.get("cmd"), str):
                    row["cmd"] = row["cmd"].replace(FULL_STATE, str(state_dir))
        checkpoint_bytes = [path.read_bytes() for path in files] if full_checkpoint else None
        if postcommit_field:
            script_source = script_source.replace(
                "trap - ERR INT TERM HUP\nif ! cleanup_originals",
                "trap - ERR INT TERM HUP\ncommit-boundary\nif ! cleanup_originals",
            )
        script.write_text(script_source.replace("/opt/fetcher", str(host)))
        state = {
            "rows": rows,
            "log": [],
            "fault": fault,
            "postcommit_field": postcommit_field,
            "command_query": command_query,
        }
        binaries = root / "bin"
        fake_daemon.install(binaries, ("docker", "sudo", "stat", "mv", "commit-boundary", "sleep"))
        account = "439622209937" if target == "staging" else "289112218471"
        registry = f"{account}.dkr.ecr.ap-southeast-1.amazonaws.com"
        environment = {
            **script_env(),
            "PATH": f"{binaries}:{os.environ['PATH']}",
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
            "FACTSET_IMAGE_REF": f"{registry}/findb/{target}/fetcher/factset@sha256:{'c' * 64}",
        }
        with fake_daemon.serve(
            state,
            lambda *call: fake_call(state, *call),
            ("FETCHER_DEPLOY_MODE", "APP_ENVIRONMENT", "FETCHER_RUNTIME_PROFILE"),
        ) as daemon:
            completed = subprocess.run(
                ["bash", str(script)],
                env={**environment, **daemon},
                capture_output=True,
                text=True,
                # Bound the harness without imposing a host-speed SLA; real
                # activation sleeps are faked and their exact count is asserted.
                timeout=90,
            )
        actual = json.loads(json.dumps(state))
        if full_checkpoint:
            actual["checkpoint_preserved"] = [
                path.read_bytes() for path in files
            ] == checkpoint_bytes
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


@pytest.mark.parametrize(
    "mode,boundary,signum,code",
    # INT/TERM/HUP share one trap; the full matrix runs once, the others pin exit codes.
    [
        *[
            (mode, boundary, "TERM", 143)
            for mode in ("candidate", "activate")
            for boundary in ("before", "after")
        ],
        ("activate", "after", "INT", 130),
        ("activate", "before", "HUP", 129),
    ],
)
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


def test_missing_installed_checkpoint_does_not_bootstrap():
    name = next(name for name, _, _, item in PROVIDERS if item == "full_market")
    completed, state, _ = execute({name: original_rows()[name]}, full_checkpoint=False)
    assert completed.returncode != 0
    assert "checkpoint_missing" in completed.stderr
    assert not state.get("provider_images")


@pytest.mark.parametrize(
    "fault",
    ["inventory-list-1", "inventory-list-81", "inventory-inspect:findb-full-market-finlab:1"],
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


# Independent expectation: existing durable host directories never move with metadata.
HOST_LAYOUT = {
    "twelve_data": ("/var/lib/findb-fetcher/historical", "-"),
    "finlab": ("/var/lib/findb-finlab-fetcher/historical", "/var/lib/findb-finlab-fetcher/cache"),
    "taifex": ("/var/lib/findb-taifex-fetcher/historical", "-"),
    "factset": ("/var/lib/findb-factset-fetcher/historical", "-"),
    "hkex": ("/var/lib/findb-hkex-fetcher/historical", "-"),
    "fmp": ("/var/lib/findb-fmp-fetcher/historical", "-"),
}


def test_inventory_derived_images_and_host_layout_are_unchanged():
    completed, state, _ = execute(original_rows())
    assert completed.returncode == 0, completed.stderr
    layouts = state["provider_layouts"]
    assert set(layouts) == {stable for stable, *_ in PROVIDERS}
    for stable, repository, provider, profile in PROVIDERS:
        image, state_dir, cache = layouts[stable]
        assert image == f"{REGISTRY}/findb/staging/fetcher/{repository}@sha256:{'c' * 64}"
        history, expected_cache = HOST_LAYOUT[provider]
        assert cache == expected_cache
        if profile == "historical":
            assert state_dir == history
    assert state["prepared_images"] == sorted(
        f"{REGISTRY}/findb/staging/fetcher/{name}@sha256:{'c' * 64}"
        for name in ("twelve-data", "finlab", "factset")
    )


def _add_example_provider(value):
    value["providers"].append("example")
    for role in ("full_market", "historical"):
        value["consumers"].append(
            {
                "name": f"findb-{role.replace('_', '-')}-example",
                "provider": "example",
                "role": role,
                "image_key": "twelve_data",
            }
        )


def _unpublished_image_key(value):
    value["images"].append("taifex")
    for consumer in value["consumers"]:
        if consumer["provider"] == "taifex":
            consumer["image_key"] = "taifex"


@pytest.mark.parametrize("change", [_add_example_provider, _unpublished_image_key])
def test_inventory_without_host_adapter_fails_before_docker(change):
    # A digest-valid generated inventory still cannot invent host state paths or
    # image references that the workflow does not pass.
    completed, state, pointer = execute(original_rows(), inventory_change=change)
    assert completed.returncode != 0
    assert "reason=provider_inventory_invalid" in completed.stderr
    assert not any(call[0] == "docker" for call in state["log"])
    assert not state.get("provider_images")
    assert pointer == "b" * 64 + "-122-1"


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
    assert len(providers) == len(PROVIDERS)
    assert all("--docker-login" not in entry for entry in providers)
    last_observation = max(i for i, entry in enumerate(state["log"]) if entry[0] == "sleep")
    first_retirement = next(
        i for i, entry in enumerate(state["log"]) if entry[:2] == ["docker", "rm"]
    )
    assert last_observation < first_retirement
    assert not any(entry[:3] == ["docker", "image", "prune"] for entry in state["log"])


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


@pytest.mark.parametrize("fault", ["new-exit-2", "new-exit-1"])
def test_a_new_consumer_that_ended_by_itself_is_discarded_and_originals_restored(fault):
    """A start the Source rejected ends the consumer with its own non-zero exit.

    That exit is why the transaction aborts; it must not also block the rollback.
    """
    original = original_rows()
    result, state, pointer = execute(original, mode="activate", fault=fault)

    assert result.returncode != 0
    assert "reason=activation_unhealthy runtime=" + PROVIDERS[0][0] in result.stderr
    assert "runtime_retirement_failed" not in result.stderr
    assert "transaction_rollback_failed" not in result.stderr
    assert_restored(original, state)
    assert all(row["running"] for row in state["rows"].values())
    assert pointer == "b" * 64 + "-122-1"


@pytest.mark.parametrize(
    "fault", ["new-exit-137", "new-oom", "new-error", "new-exit-2+unclean-stop"]
)
def test_a_new_consumer_that_was_killed_or_did_not_drain_still_fails_closed(fault):
    original = original_rows()
    result, state, _ = execute(original, mode="activate", fault=fault)

    assert result.returncode != 0
    assert "reason=runtime_retirement_failed" in result.stderr
    assert "reason=transaction_rollback_failed" in result.stderr
    assert "private-error" not in result.stderr
    # Nothing that existed before the transaction was deleted.
    kept = {row["id"] for row in state["rows"].values()}
    assert {row["id"] for row in original.values()} <= kept
