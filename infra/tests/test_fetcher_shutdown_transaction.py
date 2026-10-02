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
if command == "stat": print("root:root:700")
elif command == "mv": os.replace(args[-2], args[-1])
elif command == "sudo":
    # Runtime secret wrapper itself is not under test. No credentials are used.
    index = next(i for i, a in enumerate(args) if a.endswith("/release_fetcher_provider.sh"))
    provider, image, state_dir, state_path, stable, candidate, previous = args[index+1:index+8]
    if state.get("fault") == "provider_before": status = 7
    else:
        row = rows.get(stable)
        if row:
            row["running"] = False
            row["exit"] = row.get("stop_exit", 0)
            if row["exit"] != 0: status = 8
            else: rows[previous] = rows.pop(stable)
        if not status and os.environ["FETCHER_DEPLOY_MODE"] == "activate":
            rows[stable] = dict(id="new-"+stable, running=True, exit=0, oom=False, error="", accepted="true")
            rows[stable+"-historical"] = dict(id="new-"+stable+"-historical", running=True, exit=0, oom=False, error="", accepted="true", marker="date-boundary-v1")
        if not status and state.get("fault") == "provider_after": status = 9
elif command == "docker":
    if args[0] == "container": status = 0 if args[-1] in rows else 1
    elif args[0] == "inspect":
        row = rows.get(args[-1])
        if row is None: status = 1
        else:
            template = args[2]
            if ".State.Running" in template: print(str(row["running"]).lower())
            elif ".State.ExitCode" in template: print(row["exit"])
            elif ".State.OOMKilled" in template: print(str(row["oom"]).lower())
            elif ".State.Error" in template: print(row["error"])
            elif ".Id" in template: print(row["id"])
            elif ".Config.Image" in template: print(row.get("image", "new"))
            elif ".Config.Cmd" in template: print(row.get("cmd", ""))
            elif ".Path" in template: print(row.get("path", "findb-fetch-historical-backfill"))
            elif ".Args" in template: print(json.dumps(row.get("args", row.get("cmd", "").split()[1:]), separators=(",", ":")))
            elif "historical-shutdown" in template: print("present" if "marker" in row else "")
            elif "accepted" in template: print(row["accepted"])
            else: status = 99
    elif args[0] == "stop":
        row = rows[args[-1]]
        if row.get("stop_error"): status = 6
        else:
            row["running"] = False
            row["exit"] = row.get("stop_exit", 0)
    elif args[0] == "start":
        state.setdefault("started_ids", []).append(rows[args[-1]]["id"])
        rows[args[-1]]["running"] = True
    elif args[0] == "rm": rows.pop(args[-1])
    elif args[0] == "rename":
        if state.get("fault") == "rename_once":
            state["fault"] = "used"
            status = 5
        else: rows[args[2]] = rows.pop(args[1])
    else: status = 99
else: status = 99
store.write_text(json.dumps(state))
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
        script.write_text((source or DEPLOY.read_text()).replace("/opt/fetcher", str(host)))
        state = root / "state.json"
        state.write_text(json.dumps({"rows": rows, "log": [], "fault": fault}))
        binaries = root / "bin"
        binaries.mkdir()
        for name in ("docker", "sudo", "stat", "mv"):
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
            "DEPLOYMENT_TARGET": target,
            "ECR_REGISTRY": registry,
            "FETCHER_RELEASE_ROOT": str(release),
            "FETCHER_DEPLOY_MODE": mode,
            "FETCHER_RUNTIME_PROFILE": profile,
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
        return (
            completed,
            json.loads(state.read_text()),
            (host / "current").readlink().name,
        )


class FetcherShutdownTransactionTests(unittest.TestCase):
    def assert_restored(self, before, after):
        for name, original in before.items():
            self.assertEqual(after["rows"][name]["id"], original["id"])
            self.assertTrue(after["rows"][name]["running"], name)

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
        self.assertEqual(source.count(registration), 1)
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
                            before, mode=mode, source=source.replace(registration, replacement)
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
                    original.update(id="accepted-original", running=False, exit=0, stop_exit=0)
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
                    )
                    completed, state, pointer = execute(before, mode=mode, fault="provider_before")
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertEqual(state["rows"][retained]["id"], original["id"])
                    self.assertTrue(state["rows"][retained]["running"])
                    self.assertFalse(retained + "-previous" in state["rows"])
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
