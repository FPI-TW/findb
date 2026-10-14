"""Execute host eligibility against real release trees and offline Docker metadata."""

import base64
import copy
import gzip
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load("fetcher_host_preflight", "infra/deploy/check_fetcher_host.py")
release = load("preflight_manifest", "infra/deploy/release_manifest.py")
HISTORICAL_SOURCE_SHA = "28e9d8fa55deb5580d2bed54a4f2d717c27fb1355a7bb29da21177ebb6eb92c9"
HISTORICAL_VALIDATOR_SHA = "0fdb3091f5f9dbdd61a4a695a5f469e2b0558afc27e5eca20b1b445e484b1a38"


def historical_source(destination):
    # Full selected source is needed to prove the historical validator's real
    # checksum algorithm and materialization protocol, offline in shallow CI.
    archive = ROOT / "infra/tests/fixtures/fetcher-v0.1.5-source.tar.gz"
    assert archive.stat().st_size < 100_000
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == HISTORICAL_SOURCE_SHA
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        assert len(members) < 128
        contents = {}
        for member in members:
            assert member.isfile() and member.size < 4 * 1024 * 1024
            assert not member.name.startswith("/") and ".." not in Path(member.name).parts
            contents[member.name] = source.extractfile(member).read()
    validator_raw = contents["infra/deploy/release_manifest.py"]
    assert hashlib.sha256(validator_raw).hexdigest() == HISTORICAL_VALIDATOR_SHA
    validator_path = destination / "infra/deploy/release_manifest.py"
    validator_path.parent.mkdir(parents=True, exist_ok=True)
    validator_path.write_bytes(validator_raw)
    validator = load("historical_preflight_manifest", validator_path)
    contracts = json.loads(contents["contracts/manifest.json"])
    expected = set(validator.FETCHER_BUNDLE_FILES) | {
        "contracts/" + row["path"] for row in contracts["contracts"]
    }
    assert set(contents) == expected
    for relative, content in contents.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return validator


def production_args(command, provider=None):
    # Independent fixtures match deployed v0.1.5/v0.1.6 helper emissions.
    if command == "findb-fetch-full-market":
        return [
            command,
            "--config",
            "/app/configs/full_market.production.v1.json",
            "--state-path",
            "/var/lib/findb-full-market/state.sqlite3",
            "--provider",
            provider,
            "--readiness-file",
            f"/var/lib/findb-account/readiness/{provider}.json",
            "--run-forever",
        ]
    if command == "findb-fetch-shioaji-scheduler":
        return [
            command,
            "--manifest",
            "/app/configs/shioaji_tw50_2026_09_21.v2.json",
            "--run-forever",
        ]
    slot, dataset = {
        "findb-fetch-scheduler": ("western_markets_window", "us_equity_eod"),
        "findb-fetch-finlab-scheduler": ("taiwan_market_window", "tw_equity_eod"),
    }[command]
    return [
        command,
        "--schedule-file",
        "/app/configs/daily_scheduler.production.v3.json",
        "--slot-id",
        slot,
        "--dataset-key",
        dataset,
        "--run-forever",
    ]


REGISTRY = "289112218471.dkr.ecr.ap-southeast-1.amazonaws.com"
STAGING_REGISTRY = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
STATE_DIR = "/var/lib/findb-full-market"


class FetcherHostPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "fetcher"
        self.root = self.base / "releases" / ("a" * 64 + "-123-1")
        self.root.mkdir(parents=True, mode=0o700)
        self.root.chmod(0o700)
        (self.base / "current").symlink_to(self.root)
        for relative in release.FETCHER_BUNDLE_FILES:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, path)
        contract_path = self.root / "contracts/manifest.json"
        for contract in json.loads(contract_path.read_text())["contracts"]:
            relative = "contracts/" + contract["path"]
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, path)
        with release.SourceBundleReader(self.root) as source:
            contracts = source.load_selected_contract_manifest(contract_path)
            source_sha = source.deployment_source_bundle_sha256("fetcher")
        self.manifest = {
            "schema_version": 2,
            "fetcher_bundle_version": release.CURRENT_FETCHER_BUNDLE_VERSION,
            "deployment_target": "production",
            "unit": "fetcher",
            "commit_sha": "b" * 40,
            "images": {
                name: repository + "@sha256:" + "c" * 64
                for name, repository in release.image_repositories(
                    "production", "fetcher", REGISTRY
                ).items()
            },
            "migration_revision": "none",
            "contract_versions": list(contracts.versions),
            "contract_manifest_sha256": contracts.canonical_sha256,
            "deployment_source_bundle_sha256": source_sha,
            "created_by_run_id": "123",
            "registry": REGISTRY,
            "account_id": REGISTRY.split(".")[0],
            "release_tag": "fetcher-v0.1.6",
            "promotion_source": {
                "accepted_bundle_key": "fetcher/accepted/" + "b" * 40 + "/" + "d" * 64 + ".tar",
                "staging_registry": STAGING_REGISTRY,
            },
        }
        self.write_manifest()
        self.rows = []
        for provider, (name, command) in host.STABLES.items():
            for suffix, args in (
                ("", production_args(command)),
                (
                    "-historical",
                    [
                        "findb-fetch-historical-backfill",
                        "--provider",
                        provider,
                        "--run-forever",
                    ],
                ),
            ):
                self.rows.append(self.row(name + suffix, provider, args))
        # Every target installs the four Full workers next to the pilots.
        for provider in host.FULL_MARKET_PROVIDERS:
            self.rows.append(self.full_row(provider))

    def full_row(self, provider):
        return self.row(
            "findb-full-market-" + provider.replace("_", "-"),
            "twelve_data" if provider == "taifex" else provider,
            production_args("findb-fetch-full-market", provider),
        )

    def install_historical_source(self):
        shutil.rmtree(self.root)
        self.root.mkdir(mode=0o700)
        validator = historical_source(self.root)
        with validator.SourceBundleReader(self.root) as source:
            self.manifest["deployment_source_bundle_sha256"] = (
                source.deployment_source_bundle_sha256("fetcher")
            )
        self.manifest["release_tag"] = "fetcher-v0.1.5"
        self.write_manifest()
        return validator

    def write_manifest(self):
        (self.root / "release-manifest.json").write_text(json.dumps(self.manifest))

    def row(self, name, provider, args):
        return {
            "Name": "/" + name,
            "Path": args[0],
            "Args": args[1:],
            "Config": {
                "Image": self.manifest["images"][provider],
                "Labels": {"com.findb.fetcher.accepted": "true"},
                "Env": ["APP_ENVIRONMENT=production"],
                "Cmd": args,
                "Entrypoint": None,
            },
            "State": {
                "Running": False,
                "Status": "exited",
                "ExitCode": 0,
                "OOMKilled": False,
                "Error": "",
            },
        }

    def check(self, target="production"):
        calls = []

        def docker(command, **kwargs):
            calls.append(command)
            if command[:4] == ["docker", "container", "ls", "--all"]:
                return "\n".join(str(index) for index in range(len(self.rows)))
            if command[:2] == ["docker", "inspect"]:
                return json.dumps(self.rows)
            raise AssertionError("unexpected Docker mutation or acquisition: " + str(command))

        # macOS fixtures are user-owned. Keep actual directory/mode/source
        # checks, replacing only the root UID/GID boundary checked separately.
        def directory(path):
            self.assertTrue(path.is_dir())
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.stat().st_mode & 0o022, 0)

        with (
            patch.object(host.subprocess, "check_output", side_effect=docker),
            patch.object(host, "require_root_directory", side_effect=directory),
        ):
            result = host.check_host(target, REGISTRY, release, STATE_DIR, self.base)
        self.assertEqual(len(calls), 1 if not self.rows else 2)
        return result

    def test_old_failing_six_consumers_and_three_inert_recovery_containers(self):
        for provider, (name, _) in host.STABLES.items():
            row = self.row(name + "-historical-legacy-recovery-v015", provider, ["old-command"])
            row["Config"]["Env"] = []
            row["Config"]["Image"] = "old-immutable-image"
            row["State"]["ExitCode"] = 1
            self.rows.append(row)
        self.assertEqual(self.check(), "drained")
        self.assertNotEqual(len(self.rows), 0)  # Old empty-inventory fallback fails.
        self.assertFalse(any(row["State"]["Running"] for row in self.rows))

    def test_negative_runtime_identity_and_drain_branches(self):
        original = copy.deepcopy(self.rows)
        mutations = (
            (
                "wrong-environment",
                lambda row: row["Config"].update(Env=["APP_ENVIRONMENT=staging"]),
            ),
            (
                "legacy-only",
                lambda row: row["Config"].update(Env=["DEPLOYMENT_TARGET=production"]),
            ),
            (
                "duplicate-environment",
                lambda row: row["Config"]["Env"].append("APP_ENVIRONMENT=staging"),
            ),
            ("wrong-image", lambda row: row["Config"].update(Image="wrong-image")),
            (
                "unaccepted",
                lambda row: row["Config"].update(Labels={"com.findb.fetcher.accepted": "false"}),
            ),
            (
                "wrong-command",
                lambda row: row["Config"].update(Cmd=["unknown", "--run-forever"]),
            ),
            ("failed-exit", lambda row: row["State"].update(ExitCode=137)),
            ("oom", lambda row: row["State"].update(OOMKilled=True)),
            ("runtime-error", lambda row: row["State"].update(Error="runtime-error")),
            ("never-started", lambda row: row["State"].update(Status="created")),
        )
        for case, mutation in mutations:
            with self.subTest(case=case):
                self.rows = copy.deepcopy(original)
                mutation(self.rows[0])
                with self.assertRaises(ValueError):
                    self.check()
        self.rows = original[1:]
        with self.assertRaises(KeyError):
            self.check()
        self.rows = [self.row("unknown-fetcher", "finlab", ["unknown"])]
        with self.assertRaises(KeyError):
            self.check()
        self.rows = original
        with self.assertRaises(ValueError):
            self.check("staging")

    def test_actual_runtime_identity_and_missing_proof_fail_closed(self):
        original = copy.deepcopy(self.rows)
        mutations = (
            ("wrong-path", lambda row: row.update(Path="/bin/true")),
            ("conflicting-args", lambda row: row.update(Args=["--help"])),
            ("entrypoint-override", lambda row: row["Config"].update(Entrypoint=["/bin/true"])),
            ("missing-path", lambda row: row.pop("Path")),
            ("missing-args", lambda row: row.pop("Args")),
            ("missing-entrypoint", lambda row: row["Config"].pop("Entrypoint")),
            ("invalid-args", lambda row: row.update(Args="--run-forever")),
        )
        for consumer in range(len(original)):
            for case, mutation in mutations:
                with self.subTest(consumer=consumer, case=case):
                    self.rows = copy.deepcopy(original)
                    mutation(self.rows[consumer])
                    with self.assertRaises(ValueError):
                        self.check()
        self.rows = original
        for row in self.rows:
            row["Config"]["Entrypoint"] = []
        self.assertEqual(self.check(), "drained")

    def test_complete_argv_rejects_alternative_modes_for_every_consumer(self):
        original = copy.deepcopy(self.rows)
        self.assertEqual(self.check(), "drained")
        for index, row in enumerate(original):
            args = row["Config"]["Cmd"]
            mutations = [
                args + [flag]
                for flag in (
                    "--help",
                    "--version",
                    "--run-once",
                    "--dry-run",
                    "--check",
                    "--require-stopped",
                    "--initialize-state",
                    "--unknown",
                    "--run-forever",
                )
            ] + [
                args + ["--provider", "finlab"],
                args + ["--mode", "historical"],
                [args[0], "--run-forever", *args[1:-1]],
                [args[0], "--run-forever"],
                [args[0], *args[1:-1]],
            ]
            for changed in mutations:
                with self.subTest(consumer=row["Name"], argv=changed):
                    self.rows = copy.deepcopy(original)
                    self.rows[index]["Config"]["Cmd"] = changed
                    self.rows[index]["Args"] = changed[1:]
                    with self.assertRaises(ValueError):
                        self.check()

    def test_drained_requires_a_known_bundle_layout_identity(self):
        for version in (None, False, True, 0, 3, "1", 1.0):
            with self.subTest(version=version):
                self.manifest["fetcher_bundle_version"] = version
                if version is None:
                    self.manifest.pop("fetcher_bundle_version")
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, "format identity invalid"):
                    self.check()
        # Layout 1 is a known format, but this tree is the current layout, so
        # the recorded checksum cannot match it.
        self.manifest["fetcher_bundle_version"] = 1
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.check()
        self.manifest["fetcher_bundle_version"] = release.CURRENT_FETCHER_BUNDLE_VERSION
        self.write_manifest()
        self.assertEqual(self.check(), "drained")

    def test_pre_marker_layout_is_rejected_without_executing_installed_code(self):
        self.install_historical_source()
        with self.assertRaises(ValueError):
            self.check()
        self.manifest.pop("fetcher_bundle_version")
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "format identity invalid"):
            self.check()
        self.manifest["fetcher_bundle_version"] = 1
        self.write_manifest()
        installed_code = self.root / "infra/deploy/release_manifest.py"
        installed_code.write_text("raise RuntimeError('must never execute installed code')")
        with self.assertRaises(ValueError):
            self.check()

    def test_installed_manifest_strict_json_fails_closed(self):
        original = json.dumps(self.manifest)
        for raw in (
            '{"deployment_target":"staging",' + original[1:],
            original.replace('"schema_version": 2', '"schema_version": 1,"schema_version": 2'),
            "[]",
            "{",
        ):
            with self.subTest(raw=raw[:80]):
                (self.root / "release-manifest.json").write_text(raw)
                with self.assertRaises(ValueError):
                    self.check()
        self.write_manifest()
        self.assertEqual(self.check(), "drained")

    def test_present_historical_identity_must_match_current_release(self):
        self.rows[1]["Config"]["Image"] = "obsolete-image"
        with self.assertRaises(ValueError):
            self.check()

    def test_manifest_unit_target_registry_and_source_corruption_fail(self):
        for key, value in (
            ("unit", "findb"),
            ("deployment_target", "staging"),
            ("registry", STAGING_REGISTRY),
        ):
            with self.subTest(key=key):
                original = self.manifest[key]
                self.manifest[key] = value
                self.write_manifest()
                with self.assertRaises(ValueError):
                    self.check()
                self.manifest[key] = original
        self.write_manifest()
        artifact = self.root / "infra/deploy/runtime-secrets/fetcher.json"
        artifact.write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.check()

    def test_missing_invalid_and_symlink_release_evidence_fail(self):
        pointer = self.base / "current"
        pointer.unlink()
        with self.assertRaises(ValueError):
            self.check()
        pointer.symlink_to(self.base / "unexpected")
        with self.assertRaises(ValueError):
            self.check()
        pointer.unlink()
        pointer.symlink_to(self.root)
        manifest = self.root / "release-manifest.json"
        manifest.rename(self.root / "saved.json")
        manifest.symlink_to("saved.json")
        with self.assertRaises(ValueError):
            self.check()

    def test_running_behavior_and_empty_bootstrap_in_every_target(self):
        for target in ("staging", "production"):
            with self.subTest(target=target):
                self.rows[0]["State"]["Running"] = True
                self.assertEqual(self.check(target), "running")
        self.rows = []
        with self.assertRaises(ValueError):
            self.check()  # Missing containers with a current pointer are corrupt.
        (self.base / "current").unlink()
        for target in ("staging", "production"):
            with self.subTest(target=target):
                self.assertEqual(self.check(target), "bootstrap")
        self.rows = [self.row("unknown-service", "finlab", ["unknown"])]
        self.rows[0]["State"]["Running"] = True
        with self.assertRaises(ValueError):
            self.check()

    def test_drained_requires_all_four_full_workers_in_every_target(self):
        complete = copy.deepcopy(self.rows)
        self.assertEqual(self.check(), "drained")
        self.rows = [row for row in complete if not row["Name"].startswith("/findb-full-market-")]
        with self.assertRaises(KeyError):
            self.check()
        self.rows = [row for row in complete if not row["Name"].endswith("-historical")]
        self.assertEqual(self.check(), "drained")  # Historical workers are optional.
        self.rows = copy.deepcopy(complete)
        taifex = next(row for row in self.rows if row["Name"] == "/findb-full-market-taifex")
        taifex["Config"]["Cmd"] = [
            "findb-fetch-full-market",
            "--provider",
            "finlab",
            "--run-forever",
        ]
        with self.assertRaises(ValueError):
            self.check()  # TAIFEX shares Twelve Data's image, but cannot share its role.

    def test_layout_file_removal_and_contract_corruption_fail_closed(self):
        removed = self.root / "fetcher/configs/full_market.staging.v1.json"
        saved = removed.read_bytes()
        removed.unlink()
        with self.assertRaises(ValueError):
            self.check()
        removed.write_bytes(saved)
        self.assertEqual(self.check(), "drained")
        contracts = json.loads((self.root / "contracts/manifest.json").read_text())
        (self.root / "contracts" / contracts["contracts"][0]["path"]).write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "contract schema checksum mismatch"):
            self.check()

    def generated_preflight(self, *, historical_candidate=False, tamper=None):
        workspace = Path(self.temp.name) / "workflow"
        workspace.mkdir()
        candidate_root = workspace / "candidate"
        candidate_root.mkdir()
        if historical_candidate:
            candidate_validator = historical_source(candidate_root)
        else:
            candidate_validator = release
            for relative in release.FETCHER_BUNDLE_FILES:
                path = candidate_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, path)
            contracts = json.loads((candidate_root / "contracts/manifest.json").read_text())
            for row in contracts["contracts"]:
                path = candidate_root / "contracts" / row["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / "contracts" / row["path"], path)
        candidate_manifest = copy.deepcopy(self.manifest)
        if historical_candidate:
            # The pre-marker validator knows only its own bundle layout.
            candidate_manifest["fetcher_bundle_version"] = 1
        with candidate_validator.SourceBundleReader(candidate_root) as source:
            candidate_manifest["deployment_source_bundle_sha256"] = (
                source.deployment_source_bundle_sha256("fetcher")
            )
        manifest_path = candidate_root / "release-manifest.json"
        manifest_path.write_text(json.dumps(candidate_manifest))
        candidate_bundle = workspace / "candidate.tar"
        candidate_bundle.write_bytes(
            candidate_validator.build_bundle(candidate_root, manifest_path, "fetcher")
        )
        trusted = workspace / ".trusted-checkpoint-safety/infra/deploy"
        trusted.mkdir(parents=True)
        for name in (
            "check_fetcher_host.py",
            "release_manifest.py",
            "check_full_market_checkpoint.sh",
        ):
            shutil.copyfile(ROOT / "infra/deploy" / name, trusted / name)
        pristine = workspace / "pristine"
        shutil.copytree(trusted, pristine)
        rows_path = workspace / "rows.json"
        rows_path.write_text(json.dumps(self.rows))
        tools = workspace / "bin"
        tools.mkdir()
        shim = textwrap.dedent("""\
            import base64, hashlib, importlib.util, json, os, pathlib, shutil, stat, sys
            name = pathlib.Path(sys.argv[0]).name
            args = sys.argv[1:]
            with open(os.environ['CALL_LOG'], 'a') as log:
                log.write(json.dumps([name, *args]) + '\\n')
            if name == 'git':
                if args[2:] == ['rev-parse', 'HEAD']:
                    print('f' * 40)
                elif args[2] == 'show':
                    path = args[3].split(':', 1)[1]
                    sys.stdout.buffer.write((pathlib.Path(os.environ['PRISTINE']) / pathlib.Path(path).name).read_bytes())
                else:
                    raise SystemExit(91)
            elif name == 'mktemp':
                if sys.platform == 'darwin':
                    args = [arg.replace('/tmp/', '/private/tmp/') for arg in args]
                os.execv('/usr/bin/mktemp', ['mktemp', *args])
            elif name == 'base64':
                if args == ['--decode']:
                    sys.stdout.buffer.write(base64.b64decode(sys.stdin.buffer.read()))
                elif args[0] == '-w0':
                    data = pathlib.Path(args[1]).read_bytes() if len(args) == 2 else sys.stdin.buffer.read()
                    sys.stdout.buffer.write(base64.b64encode(data))
                else:
                    raise SystemExit(92)
            elif name == 'aws':
                if args[:2] == ['sts', 'get-caller-identity']:
                    print(os.environ['AWS_ACCOUNT_ID'])
                elif args[:2] == ['s3api', 'get-object']:
                    shutil.copyfile(os.environ['CANDIDATE_BUNDLE'], args[-1])
                elif args[:2] == ['ecr', 'get-login-password']:
                    print('offline-fixture-password')
                elif args[:2] == ['secretsmanager', 'get-secret-value']:
                    raise SystemExit(1)
                else:
                    raise SystemExit(93)
            elif name == 'docker':
                if args[:4] == ['container', 'ls', '--all', '--format']:
                    print('\\n'.join(str(i) for i, _ in enumerate(json.loads(pathlib.Path(os.environ['ROWS']).read_text()))))
                elif args[0] == 'inspect':
                    print(pathlib.Path(os.environ['ROWS']).read_text())
                elif args[:2] == ['image', 'inspect']:
                    print(args[2])
                elif args[0] == '--config' and args[2] == 'login':
                    assert '--password-stdin' in args
                    # Real Docker drains the password pipe before returning.
                    # An early fake exit races the AWS shim's stdout flush on Linux.
                    assert sys.stdin.read() == 'offline-fixture-password\\n'
                elif args[0] != '--config' or args[2] != 'pull':
                    raise SystemExit(94)
            elif name == 'getent':
                pass
            elif name == 'python3':
                if args[0].endswith('/load_runtime_secrets.py'):
                    assert '--check-only' in args
                elif args[0].endswith('/host-guard.py'):
                    spec = importlib.util.spec_from_file_location('transported_guard', args[0])
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    original = module.check_host
                    def check(target, registry, validator, state_dir):
                        return original(target, registry, validator, state_dir, pathlib.Path(os.environ['INSTALLED_BASE']))
                    def directory(path):
                        info = path.lstat()
                        assert stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022
                    module.check_host = check
                    module.require_root_directory = directory
                    sys.argv = args
                    module.main()
                else:
                    os.execv(sys.executable, [sys.executable, *args])
            else:
                raise SystemExit(95)
            """)
        for name in ("git", "base64", "aws", "docker", "getent", "python3", "mktemp"):
            path = tools / name
            path.write_text("#!" + sys.executable + "\n" + shim)
            path.chmod(0o700)
        env = {
            **os.environ,
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "WORKFLOW_SHA": "f" * 40,
            "PRISTINE": str(pristine),
            "CANDIDATE_BUNDLE": str(candidate_bundle),
            "INSTALLED_BASE": str(self.base),
            "ROWS": str(rows_path),
            "CALL_LOG": str(workspace / "calls.jsonl"),
            "TARGET": "production",
            "REVISION": self.manifest["commit_sha"],
            "BUNDLE_KEY": "offline-fixture-key",
            "BUNDLE_SHA256": hashlib.sha256(candidate_bundle.read_bytes()).hexdigest(),
            "VALIDATOR_SHA256": hashlib.sha256(
                (candidate_root / "infra/deploy/release_manifest.py").read_bytes()
            ).hexdigest(),
            "TWELVE_IMAGE": self.manifest["images"]["twelve_data"],
            "FINLAB_IMAGE": self.manifest["images"]["finlab"],
            "SHIOAJI_IMAGE": self.manifest["images"]["shioaji"],
            "TARGET_ID": "offline-instance",
            "AWS_REGION": "ap-southeast-1",
            "AWS_ACCOUNT_ID": REGISTRY.split(".")[0],
            "ECR_REGISTRY": REGISTRY,
            "BUNDLE_BUCKET": "offline-bucket",
            "DNS_CHECK_NAME": "offline.invalid",
            "SSM_LOG_GROUP": "offline-log",
            "FULL_MARKET_STATE_DIR": STATE_DIR,
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        workflow = (ROOT / ".github/workflows/fetcher-deploy.yml").read_text()

        def step(name):
            block = re.search(
                r"- name: " + re.escape(name) + r"(?P<body>.*?)(?=\n      - name:)",
                workflow,
                re.S,
            )["body"]
            return textwrap.dedent(block.split("        run: |\n", 1)[1])

        if tamper in ("provenance", "guard-provenance"):
            helper = (
                "check_fetcher_host.py" if tamper == "guard-provenance" else "release_manifest.py"
            )
            (trusted / helper).write_text("raise RuntimeError('untrusted')")
        provenance = subprocess.run(
            ["bash", "-c", step("Verify trusted checkpoint safety provenance")],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
        )
        if provenance.returncode:
            return provenance
        generation = step("Run read-only SSM preflight").split('host_command="$(printf', 1)[0]
        generated = subprocess.run(
            ["bash", "-c", generation + '\nprintf "%s" "$host_command"'],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if tamper == "guard-transport":
            # Alter payload after generation without altering its expected SHA.
            payload = base64.b64encode(
                subprocess.check_output(["gzip", "-c", str(trusted / "check_fetcher_host.py")])
            ).decode()
            generated = generated.replace(
                payload,
                base64.b64encode(gzip.compress(b"raise RuntimeError('tamper')")).decode(),
            )
        if tamper == "validator-transport":
            payload = base64.b64encode(
                subprocess.check_output(["gzip", "-c", str(trusted / "release_manifest.py")])
            ).decode()
            generated = generated.replace(
                payload,
                base64.b64encode(gzip.compress(b"raise RuntimeError('tamper')")).decode(),
            )
        self.assertLess(len(generated.encode()), 24 * 1024)
        result = subprocess.run(
            ["bash", "-c", generated],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
        )
        calls = [json.loads(line) for line in (workspace / "calls.jsonl").read_text().splitlines()]
        for call in calls:
            self.assertFalse(
                call[:2] in (["docker", "start"], ["docker", "restart"], ["docker", "run"])
            )
        return result

    def test_generated_layout_one_accepted_candidate_passes_host_preflight(self):
        historical = historical_source(Path(self.temp.name) / "historical-validator-proof")
        with historical.SourceBundleReader(self.root) as source:
            contracts = source.load_selected_contract_manifest(
                self.root / "contracts/manifest.json"
            )
            with self.assertRaises(ValueError):
                historical.validate_manifest(self.manifest, contracts, source)
        self.rows = [row for row in self.rows if not row["Name"].endswith("-historical")]
        # The trusted validator still reconstructs bundle layout 1, so the host
        # preflight accepts the older accepted candidate on a drained host. The
        # environment-contract gate, not this preflight, refuses pre-marker
        # bundles before their coordinator runs.
        result = self.generated_preflight(historical_candidate=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fetcher_host_eligibility=drained", result.stdout)

    def test_generated_new_candidate_rejects_pre_marker_installed_host(self):
        # A pre-marker installation predates the unified bundle layouts and is
        # no longer a replay target; the drained proof fails closed.
        self.install_historical_source()
        result = self.generated_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fetcher_host_eligibility=failed", result.stderr)

    def test_generated_wrong_installed_identity_fails(self):
        self.rows[0]["Config"]["Image"] = "wrong-image"
        result = self.generated_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fetcher_host_eligibility=failed", result.stderr)

    def test_generated_duplicate_installed_manifest_identity_fails(self):
        raw = json.dumps(self.manifest)
        (self.root / "release-manifest.json").write_text(
            '{"deployment_target":"staging",' + raw[1:]
        )
        result = self.generated_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fetcher_host_eligibility=failed", result.stderr)

    def test_generated_actual_runtime_override_fails(self):
        self.rows[0]["Config"]["Entrypoint"] = ["/bin/true"]
        self.rows[0]["Path"] = "/bin/true"
        result = self.generated_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fetcher_host_eligibility=failed", result.stderr)

    def test_generated_corrupt_installed_source_fails(self):
        (self.root / "infra/deploy/runtime-secrets/fetcher.json").write_text("corrupt")
        result = self.generated_preflight()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fetcher_host_eligibility=failed", result.stderr)

    def test_generated_trusted_provenance_fails_before_execution(self):
        result = self.generated_preflight(tamper="provenance")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("untrusted", result.stderr)

    def test_generated_guard_provenance_fails_before_execution(self):
        result = self.generated_preflight(tamper="guard-provenance")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("untrusted", result.stderr)

    def test_generated_guard_transport_checksum_fails_before_execution(self):
        result = self.generated_preflight(tamper="guard-transport")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertIn("status=failed", result.stdout)

    def test_generated_installed_validator_checksum_fails_before_execution(self):
        result = self.generated_preflight(tamper="validator-transport")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertIn("status=failed", result.stdout)

    def test_docker_query_failure_is_not_absence(self):
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                outputs = ["container-id", json.dumps(self.rows)]
                outputs[fail_at - 1] = subprocess.CalledProcessError(1, "docker")
                with patch.object(host.subprocess, "check_output", side_effect=outputs):
                    with self.assertRaises(subprocess.CalledProcessError):
                        host.check_host("production", REGISTRY, release, STATE_DIR, self.base)

    def test_root_directory_rejects_symlink_and_writable_directory(self):
        with self.assertRaises(ValueError):
            host.require_root_directory(self.base / "current")
        self.root.chmod(0o777)
        with self.assertRaises(ValueError):
            host.require_root_directory(self.root)


if __name__ == "__main__":
    unittest.main()
