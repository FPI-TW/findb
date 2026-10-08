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
import unittest
from pathlib import Path

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


class FetcherShutdownTransactionTests(unittest.TestCase):
    def test_cmd_query_unknown_role_rejects_before_any_effect_in_both_entries(self):
        legacy = PROVIDERS[0][0]
        invalid = [
            "",
            "private-unverified-command",
            "null",
            "[]",
            "{}",
            '"findb-fetch-full-market"',
            "[null]",
            "[1]",
            '[""]',
            '["unknown-command"]',
            '["findb-fetch-full-market",{}]',
        ]
        guard = (ROOT / "infra/deploy/check_full_market_checkpoint.sh").read_text()
        for entry in ["coordinator", "guard"]:
            for output in invalid + ['["findb-fetch-full-market"]']:
                with self.subTest(entry=entry, output=output):
                    before = {
                        name: row
                        for name, row in original_rows().items()
                        if not name.endswith("-historical")
                    }
                    before[legacy].update(
                        cmd=["findb-fetch-full-market"], image="installed-full-image"
                    )
                    completed, actual, pointer = execute(
                        copy.deepcopy(before),
                        target="production",
                        mode="activate",
                        source=guard if entry == "guard" else None,
                        command_query=(legacy, output),
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    reason = (
                        "full_market_checkpoint_missing"
                        if output == '["findb-fetch-full-market"]'
                        else "command_unknown"
                    )
                    self.assertIn(reason, completed.stderr)
                    self.assertNotIn("private-unverified-command", completed.stderr)
                    self.assertEqual(before, actual["rows"])
                    self.assertEqual(pointer, "b" * 64 + "-122-1")
                    self.assertFalse(
                        any(
                            call[0] == "mv"
                            or call[0] == "sudo"
                            and "--consumer" in call
                            or call[:2]
                            in [
                                ["docker", "stop"],
                                ["docker", "start"],
                                ["docker", "rename"],
                                ["docker", "rm"],
                            ]
                            for call in actual["log"]
                        )
                    )

    def test_known_cmd_roles_and_full_checkpoint_are_preserved_in_both_entries(self):
        legacy = PROVIDERS[0][0]
        guard = (ROOT / "infra/deploy/check_full_market_checkpoint.sh").read_text()
        commands = [
            ["findb-fetch-scheduler", "--note", "findb-fetch-full-market"],
            ["findb-fetch-finlab-scheduler", "--help"],
            ["findb-fetch-shioaji-scheduler", "--check"],
            ["findb-fetch-taifex-pilot"],
            ["findb-fetch-historical-backfill", "--provider", "twelve_data", "--run-forever"],
            ["python", "-m", "findb_fetcher"],
            ["findb-fetch-full-market", "--config", "/app/configs/full_market.production.v1.json"],
        ]
        for entry in ["coordinator", "guard"]:
            for command in commands:
                # Other roles are validated by the standalone gate; ordinary
                # coordinator continuation separately exercises actual Pilot.
                if entry == "coordinator" and command[0] not in [
                    "findb-fetch-scheduler",
                    "findb-fetch-full-market",
                ]:
                    continue
                with self.subTest(entry=entry, command=command):
                    before = {
                        name: row
                        for name, row in original_rows().items()
                        if not name.endswith("-historical")
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
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    if full:
                        self.assertTrue(actual["checkpoint_preserved"])
                    if entry == "guard":
                        self.assertEqual(before, actual["rows"])
                    else:
                        full_calls = [
                            call
                            for call in actual.get("provider_images", [])
                            if call[0].startswith("findb-full-market-")
                        ]
                        self.assertEqual(bool(full_calls), full)

    def test_valid_full_cmd_invalid_checkpoint_has_zero_effects_in_both_entries(self):
        legacy = PROVIDERS[0][0]
        guard = (ROOT / "infra/deploy/check_full_market_checkpoint.sh").read_text()
        for entry in ["coordinator", "guard"]:
            for kind in ["corrupt", "unusable"]:
                with self.subTest(entry=entry, kind=kind):
                    before = {
                        name: row
                        for name, row in original_rows().items()
                        if not name.endswith("-historical")
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
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("full_market_checkpoint_invalid", completed.stderr)
                    self.assertTrue(actual["checkpoint_preserved"])
                    self.assertEqual(before, actual["rows"])
                    self.assertEqual(pointer, "b" * 64 + "-122-1")
                    self.assertFalse(
                        any(
                            call[0] == "mv"
                            or call[0] == "sudo"
                            and "--consumer" in call
                            or call[:2]
                            in [
                                ["docker", "stop"],
                                ["docker", "start"],
                                ["docker", "rename"],
                                ["docker", "rm"],
                            ]
                            for call in actual["log"]
                        )
                    )

    def test_whole_entry_unknown_inventory_precedes_every_external_effect(self):
        for running in [False, True]:
            for name in ["findb-full-market-twelve-data", "findb-full-market-twelve-data-previous"]:
                for rc in [1, 81]:
                    for query in ["list", "inspect"]:
                        with self.subTest(running=running, name=name, rc=rc, query=query):
                            before = original_rows()
                            before[name] = dict(
                                id="accepted-full",
                                running=running,
                                exit=0,
                                oom=False,
                                error="",
                                accepted="true",
                            )
                            fault = (
                                f"inventory-list-{rc}"
                                if query == "list"
                                else f"inventory-inspect:{name}:{rc}"
                            )
                            completed, state, _ = execute(copy.deepcopy(before), fault=fault)
                            self.assertNotEqual(completed.returncode, 0)
                            self.assertIn("reason=query_unknown", completed.stderr)
                            self.assertEqual(before, state["rows"])
                            self.assertFalse(
                                any(
                                    a[0] == "sudo"
                                    or (
                                        a[0] == "docker"
                                        and a[1]
                                        in ["stop", "start", "rm", "rename", "run", "create"]
                                    )
                                    for a in state["log"]
                                )
                            )

    def test_initial_historical_and_interrupted_inventory_has_zero_effects(self):
        for suffix in ["", "-previous", "-candidate", "-transaction-backup", "-preflight-control"]:
            for running in [False, True]:
                for rc in [1, 81]:
                    with self.subTest(suffix=suffix, running=running, rc=rc):
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
                        self.assertNotEqual(completed.returncode, 0)
                        self.assertIn("reason=query_unknown", completed.stderr)
                        self.assertEqual(before, state["rows"])
                        self.assertEqual(pointer, "b" * 64 + "-122-1")
                        self.assertFalse(
                            any(
                                call[0] in ["sudo", "mv"]
                                or (
                                    call[0] == "docker"
                                    and call[1]
                                    in ["stop", "start", "rm", "rename", "run", "create"]
                                )
                                for call in state["log"]
                            )
                        )

    def test_initial_historical_field_queries_reject_before_effects(self):
        name = PROVIDERS[1][0] + "-historical"
        cases = [
            (field, failure)
            for field in [
                ".Id",
                ".Config.Image",
                ".State.Running",
                ".State.ExitCode",
                ".State.OOMKilled",
                ".State.Error",
            ]
            for failure in ["rc1", "rc81"]
        ]
        for field, failure in cases:
            with self.subTest(field=field, failure=failure):
                before = original_rows()
                completed, state, pointer = execute(
                    copy.deepcopy(before),
                    mode="activate",
                    fault=f"initial-field:{name}:{field}:{failure}",
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(state["rows"], before)
                self.assertEqual(pointer, "b" * 64 + "-122-1")
                self.assertFalse(
                    any(
                        call[0] in ["sudo", "mv"]
                        or (
                            call[0] == "docker"
                            and call[1] in ["stop", "start", "rm", "rename", "run", "create"]
                        )
                        for call in state["log"]
                    )
                )
        for changed in [
            {"id": ""},
            {"image": ""},
            {"running": "invalid"},
            {"exit": ""},
            {"oom": "invalid"},
        ]:
            with self.subTest(changed=changed):
                before = original_rows()
                before[name].update(changed)
                completed, state, pointer = execute(copy.deepcopy(before), mode="activate")
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(state["rows"], before)
                self.assertEqual(pointer, "b" * 64 + "-122-1")
                self.assertFalse(
                    any(
                        call[0] in ["sudo", "mv"]
                        or (
                            call[0] == "docker"
                            and call[1] in ["stop", "start", "rm", "rename", "run", "create"]
                        )
                        for call in state["log"]
                    )
                )

    def test_previous_only_historical_retires_before_replacement_and_restores_on_failure(self):
        for running in [False, True]:
            for fault in ["", "provider_before", "provider_after", "provider_signal"]:
                with self.subTest(running=running, fault=fault):
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
                        self.assertNotEqual(completed.returncode, 0, completed.stderr)
                        self.assert_restored(before, actual)
                        self.assertEqual(pointer, "b" * 64 + "-122-1")
                        self.assertNotIn("transaction_rollback_failed", completed.stderr)
                    else:
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        self.assertTrue(actual["rows"][stable]["running"])
                        self.assertNotIn(previous, actual["rows"])
                        creation = next(
                            i
                            for i, call in enumerate(actual["log"])
                            if call[0] == "sudo" and "--consumer" in call
                        )
                        if running:
                            stop = actual["log"].index(["docker", "stop", "--time", "30", previous])
                            self.assertLess(stop, creation)
                        validations = [
                            i
                            for i, call in enumerate(actual["log"])
                            if call[:2] == ["docker", "inspect"]
                            and call[-1] == previous
                            and "State.Running" in call[3]
                        ]
                        self.assertTrue(any(i < creation for i in validations))

    def test_bounded_replay_image_comes_from_registered_accepted_original(self):
        for mode in ["stable", "previous-only", "interrupted-stable"]:
            with self.subTest(mode=mode):
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
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn([stable, image], actual["provider_images"])
                self.assertNotIn(
                    [stable, before.get(stable, {}).get("image")],
                    actual["provider_images"] if mode == "interrupted-stable" else [],
                )
                self.assertEqual(pointer, "a" * 64 + "-123-1")

    def test_legacy_full_migration_selects_accepted_original_before_command(self):
        pilot = PROVIDERS[0][0]
        full = "findb-full-market-twelve-data"
        image = "installed-full-image"
        cases = [
            (command, replay, mode, mode == "candidate", "")
            for command in ["findb-fetch-scheduler", "findb-fetch-full-market"]
            for replay in [False, True]
            for mode in ["candidate", "activate"]
        ]
        cases += [
            (None, False, "candidate", False, ""),
            ("accepted-stable", True, "activate", True, ""),
        ]
        cases += [
            ("findb-fetch-scheduler", True, "activate", True, fault)
            for fault in [
                "provider_before",
                "provider_after",
                "provider_signal",
                "stop-refused",
                "query-unknown",
            ]
        ]
        for command, replay, mode, running, fault in cases:
            with self.subTest(
                command=command, replay=replay, mode=mode, running=running, fault=fault
            ):
                before = {
                    name: row
                    for name, row in original_rows().items()
                    if not name.endswith("-historical")
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
                self.assertTrue(actual["checkpoint_preserved"])
                self.assertFalse(
                    any(call[:2] == ["docker", "rm"] and "-f" in call for call in actual["log"])
                )
                self.assertNotIn("interrupted-stable", actual.get("started_ids", []))
                if fault or mode == "candidate":
                    self.assertNotEqual(completed.returncode, 0) if fault else self.assertEqual(
                        completed.returncode, 0, completed.stderr
                    )
                    self.assert_restored(expected, actual)
                    self.assertEqual(actual["rows"][original_name]["image"], image)
                    self.assertNotIn("transaction_rollback_failed", completed.stderr)
                    self.assertEqual(pointer, "b" * 64 + "-122-1")
                    self.assertFalse(
                        any(
                            call[:2] == ["docker", "rm"]
                            and call[-1] in [original_name, "accepted-legacy-full"]
                            for call in actual["log"]
                        )
                    )
                else:
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertIn("fetcher_aws_deploy=activated", completed.stdout)
                    self.assertTrue(actual["rows"][full]["running"])
                    self.assertEqual(pointer, "a" * 64 + "-123-1")
                if fault == "query-unknown":
                    self.assertEqual(before, actual["rows"])
                    self.assertFalse(
                        any(
                            call[0] == "mv"
                            or call[0] == "sudo"
                            and "--consumer" in call
                            or call[:2]
                            in [
                                ["docker", "stop"],
                                ["docker", "rename"],
                                ["docker", "rm"],
                                ["docker", "start"],
                            ]
                            for call in actual["log"]
                        )
                    )
                elif fault != "stop-refused":
                    full_calls = [
                        call for call in actual.get("provider_images", []) if call[0] == full
                    ]
                    self.assertTrue(full_calls)
                    expected_image = (
                        image
                        if replay
                        else f"289112218471.dkr.ecr.ap-southeast-1.amazonaws.com/findb/production/fetcher/twelve-data@sha256:{'c' * 64}"
                    )
                    self.assertEqual(full_calls[0][1], expected_image)

    def test_postcommit_cleanup_failure_preserves_committed_replacement_and_old_ids(self):
        fields = [
            (".State.ExitCode", "137"),
            (".State.OOMKilled", "true"),
            (".State.Error", "private-host-error"),
        ]
        fields += [
            (field, value)
            for field in [
                ".Id",
                ".Config.Image",
                ".State.Running",
                ".State.ExitCode",
                ".State.OOMKilled",
                ".State.Error",
            ]
            for value in ["rc1", "rc81", "invalid", "empty"]
            if (field, value) != (".State.Error", "empty")
        ]
        for field, value in fields:
            with self.subTest(field=field, value=value):
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
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("committed_cleanup_failed committed=true", completed.stderr)
                self.assertNotIn("transaction_aborted", completed.stderr)
                self.assertNotIn("fetcher_aws_deploy=activated", completed.stdout)
                self.assertNotIn("private-host-error", completed.stderr)
                self.assertEqual(pointer, "a" * 64 + "-123-1")
                self.assertTrue(
                    {row["id"] for row in before.values()}
                    <= {row["id"] for row in actual["rows"].values()}
                )
                self.assertTrue(actual["rows"][PROVIDERS[0][0]]["running"])
                self.assertFalse(any(call[:2] == ["docker", "rm"] for call in actual["log"]))

    def test_postcommit_matching_identity_stdout_nonzero_preserves_old_ids(self):
        for field, rc in [(".Id", 1), (".Id", 81), (".Config.Image", 1), (".Config.Image", 81)]:
            with self.subTest(field=field, rc=rc):
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
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("committed_cleanup_failed committed=true", completed.stderr)
                self.assertNotIn("fetcher_aws_deploy=activated", completed.stdout)
                self.assertNotIn("transaction_aborted", completed.stderr)
                self.assertTrue(actual["checkpoint_preserved"])
                self.assertTrue(actual["rows"][PROVIDERS[0][0]]["running"])
                self.assertEqual(pointer, "a" * 64 + "-123-1")
                self.assertTrue(
                    {row["id"] for row in before.values()}
                    <= {row["id"] for row in actual["rows"].values()}
                )
                self.assertFalse(any(call[:2] == ["docker", "rm"] for call in actual["log"]))

    def test_replay_unknown_accepted_metadata_rejects_before_runtime_effects(self):
        for failure in ["invalid-label", "rc1", "rc81"]:
            with self.subTest(failure=failure):
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
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(before, actual["rows"])
                self.assertEqual(pointer, "b" * 64 + "-122-1")
                self.assertFalse(
                    any(
                        call[0] == "sudo"
                        and "--consumer" in call
                        or call[:2]
                        in [
                            ["docker", "stop"],
                            ["docker", "rename"],
                            ["docker", "rm"],
                            ["docker", "start"],
                        ]
                        for call in actual["log"]
                    )
                )

    def test_historical_interrupted_stable_preserves_selected_previous(self):
        for running in [False, True]:
            for fault in ["", "provider_after"]:
                with self.subTest(running=running, fault=fault):
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
                        self.assertNotEqual(completed.returncode, 0)
                        self.assert_restored(expected, actual)
                        self.assertNotIn("transaction_rollback_failed", completed.stderr)
                    else:
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        self.assertEqual(actual["rows"][stable]["id"], "new-" + stable)
                        self.assertNotIn(previous, actual["rows"])
                    self.assertNotIn("interrupted-historical", actual.get("started_ids", []))

    def test_accepted_legacy_historical_137_passes_postcommit_cleanup(self):
        completed, actual, pointer = execute(original_rows(), mode="activate")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("fetcher_aws_deploy=activated", completed.stdout)
        self.assertEqual(pointer, "a" * 64 + "-123-1")
        self.assertFalse(any(row["id"].startswith("old-") for row in actual["rows"].values()))

    def assert_restored(self, before, after):
        for name, original in before.items():
            self.assertEqual(after["rows"][name]["id"], original["id"])
            self.assertEqual(after["rows"][name]["running"], original["running"], name)

    def test_legacy_retirement_and_candidate_restores_originals(self):
        before = original_rows()
        completed, state, pointer = execute(before)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assert_restored(before, state)
        self.assertEqual(pointer, "b" * 64 + "-122-1")
        self.assertEqual(completed.stderr.count("legacy_historical_crash_recovery"), 3)

    def test_retry_handles_previously_stopped_legacy_137(self):
        before = original_rows()
        before[PROVIDERS[0][0] + "-historical"].update(running=False, exit=137)
        completed, state, _ = execute(before)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assert_restored(before, state)

    def test_legacy_exception_does_not_apply_to_production_or_full_market(self):
        for profile in ("bounded", "full-market"):
            with self.subTest(profile=profile):
                before = original_rows()
                completed, state, _ = execute(before, target="production", profile=profile)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("runtime_retirement_failed", completed.stderr)
                self.assert_restored(before, state)

    def test_absent_historical_is_normal(self):
        before = original_rows()
        del before[PROVIDERS[0][0] + "-historical"]
        completed, state, _ = execute(before)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assert_restored(before, state)

    def test_failed_provider_transactions_restore_containers_and_pointer(self):
        for mode in ("candidate", "activate"):
            for fault in ("provider_before", "provider_after", "rename_once"):
                with self.subTest(mode=mode, fault=fault):
                    before = original_rows()
                    completed, state, pointer = execute(before, mode=mode, fault=fault)
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("transaction_aborted", completed.stderr)
                    self.assert_restored(before, state)
                    self.assertEqual(pointer, "b" * 64 + "-122-1")

    def test_nonlegacy_or_unsafe_retirement_aborts_and_recovers(self):
        for change in (
            {"oom": True},
            {"error": "sensitive-host-error"},
            {"stop_exit": 1},
            {"marker": "date-boundary-v1"},
            {"image": "unknown@sha256:" + "d" * 64},
            {"cmd": "findb-fetch-scheduler --run-forever"},
            {"accepted": "false"},
            {"path": "unexpected-entrypoint"},
            {"args": ["--provider", "twelve_data", "--run-forever", "--extra"]},
            {"cmd": "findb-fetch-historical-backfill --provider finlab --run-forever"},
            {"stop_error": True},
        ):
            with self.subTest(change=change):
                before = original_rows()
                before[PROVIDERS[0][0] + "-historical"].update(change)
                completed, state, _ = execute(before)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("transaction_aborted", completed.stderr)
                self.assertNotIn("sensitive-host-error", completed.stderr)
                if change.get("accepted") != "false":
                    self.assert_restored(before, state)

    def test_new_historical_clean_exit_activates(self):
        before = original_rows()
        for name, row in before.items():
            if name.endswith("-historical"):
                row.update(marker="date-boundary-v1", stop_exit=0)
        completed, state, pointer = execute(before, mode="activate")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(pointer, "a" * 64 + "-123-1")
        self.assertFalse(any(name.endswith("-previous") for name in state["rows"]))
        self.assertTrue(all(row["id"].startswith("new-") for row in state["rows"].values()))
        self.assertEqual(
            set(state["rows"]), {row[0] for row in PROVIDERS} | {"findb-fetcher-taifex-scheduler"}
        )
        self.assertFalse(any(name.endswith("-historical") for name in state["rows"]))

    def test_commit_preparation_rejects_unsafe_original_before_removal(self):
        for changed in [{"oom": True}, {"error": "private-original-error"}]:
            with self.subTest(changed=changed):
                before = original_rows()
                for name, row in before.items():
                    if name.endswith("-historical"):
                        row.update(marker="date-boundary-v1", stop_exit=0)
                before[PROVIDERS[0][0]].update(changed)
                completed, state, pointer = execute(copy.deepcopy(before), mode="activate")
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("transaction_aborted", completed.stderr)
                self.assertNotIn("private-original-error", completed.stderr)
                self.assert_restored(before, state)
                self.assertEqual(pointer, "b" * 64 + "-122-1")
                original_ids = {row["id"] for row in before.values()}
                removals = [call[-1] for call in state["log"] if call[:2] == ["docker", "rm"]]
                self.assertTrue(original_ids.isdisjoint(removals))

    def test_ordinary_scheduler_137_remains_failure(self):
        before = original_rows()
        before[PROVIDERS[0][0]]["stop_exit"] = 137
        completed, state, pointer = execute(before, mode="activate")
        self.assertNotEqual(completed.returncode, 0)
        self.assert_restored(before, state)
        self.assertEqual(pointer, "b" * 64 + "-122-1")

    def test_retirement_failure_does_not_restore_stale_previous(self):
        before = original_rows()
        historical = PROVIDERS[0][0] + "-historical"
        before[historical]["stop_exit"] = 1
        before[historical + "-previous"] = copy.deepcopy(before[historical])
        before[historical + "-previous"].update(id="stale", running=False)
        completed, state, _ = execute(before)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(state["rows"][historical]["id"], before[historical]["id"])
        self.assertTrue(state["rows"][historical]["running"])

    def test_signals_before_and_after_registration_restore_prior_work_and_pointer(self):
        source = DEPLOY.read_text()
        registration = '  processed+=("${stable}:${previous}:${old_available}:${original_id}")'
        start = source.index("register_named_full() {")
        end = source.index("\n}\n", start) + len("\n}")
        function = source[start:end]
        self.assertEqual(function.count(registration), 1)
        second = PROVIDERS[1][0] + "-historical"
        for mode in ("candidate", "activate"):
            for boundary in ("before", "after"):
                for signum, code in (("INT", 130), ("TERM", 143), ("HUP", 129)):
                    with self.subTest(mode=mode, boundary=boundary, signum=signum):
                        injection = f'  if [ "$stable" = {second} ]; then kill -{signum} "$$"; fi'
                        replacement = (
                            injection + "\n" + registration
                            if boundary == "before"
                            else registration + "\n" + injection
                        )
                        before = original_rows()
                        completed, state, pointer = execute(
                            before,
                            mode=mode,
                            source=source[:start]
                            + function.replace(registration, replacement)
                            + source[end:],
                        )
                        self.assertEqual(completed.returncode, code, completed.stderr)
                        self.assertIn("transaction_aborted", completed.stderr)
                        self.assertNotIn("rollback_failed", completed.stderr)
                        self.assert_restored(before, state)
                        self.assertEqual(pointer, "b" * 64 + "-122-1")
                        self.assertFalse(any(name.endswith("-previous") for name in state["rows"]))
                        # First retirement occurred; the current registration
                        # boundary precedes any mutation of the second runtime.
                        renames = [
                            call for call in state["log"] if call[:2] == ["docker", "rename"]
                        ]
                        self.assertIn(PROVIDERS[0][0] + "-historical", renames[0])
                        self.assertFalse(any(second in call for call in renames))

    def test_unaccepted_stable_never_displaces_accepted_retained_original(self):
        for retained in (PROVIDERS[0][0] + "-historical", "findb-fetcher-taifex-scheduler"):
            for mode in ("candidate", "activate"):
                with self.subTest(retained=retained, mode=mode):
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
                    original.update(
                        id="accepted-original", running=False, exit=0, stop_exit=0, cmd=command
                    )
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
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertEqual(state["rows"][retained + "-previous"]["id"], original["id"])
                    self.assertFalse(state["rows"][retained + "-previous"]["running"])
                    self.assertNotIn(retained, state["rows"])
                    self.assertEqual(pointer, "b" * 64 + "-122-1")
                    operations = state["log"]
                    self.assertNotIn(["docker", "rm", retained + "-previous"], operations)
                    self.assertNotIn(
                        ["docker", "rename", retained, retained + "-previous"], operations
                    )
                    self.assertNotIn("unaccepted-stable", state.get("started_ids", []))

    def test_unaccepted_retained_without_stable_fails_closed_without_starting_it(self):
        for retained in (PROVIDERS[0][0] + "-historical", "findb-fetcher-taifex-scheduler"):
            for mode in ("candidate", "activate"):
                with self.subTest(retained=retained, mode=mode):
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
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("unaccepted_retained_runtime", completed.stderr)
                    self.assertNotIn(retained, state["rows"])
                    self.assertEqual(state["rows"][retained + "-previous"], false_previous)
                    self.assertEqual(pointer, "b" * 64 + "-122-1")
                    starts = [call[-1] for call in state["log"] if call[:2] == ["docker", "start"]]
                    self.assertNotIn(retained, starts)
                    self.assertNotIn(retained + "-previous", starts)
                    self.assertNotIn("unaccepted-previous", state.get("started_ids", []))

    def test_unaccepted_stable_strict_stop_failure_preserves_accepted_previous(self):
        retained = PROVIDERS[0][0] + "-historical"
        for change in ({"stop_exit": 137}, {"oom": True}, {"error": "sensitive-error"}):
            with self.subTest(change=change):
                before = original_rows()
                original = copy.deepcopy(before[retained])
                original.update(id="accepted-original", running=False, exit=0, stop_exit=0)
                before[retained + "-previous"] = original
                before[retained].update(accepted="false", stop_exit=0, marker="date-boundary-v1")
                before[retained].update(change)
                completed, state, pointer = execute(before, mode="activate")
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("runtime_retirement_failed", completed.stderr)
                self.assertEqual(state["rows"][retained + "-previous"]["id"], original["id"])
                self.assertEqual(pointer, "b" * 64 + "-122-1")
                self.assertNotIn("sensitive-error", completed.stderr)
                starts = [call[-1] for call in state["log"] if call[:2] == ["docker", "start"]]
                self.assertNotIn(retained, starts)


if __name__ == "__main__":
    unittest.main()
