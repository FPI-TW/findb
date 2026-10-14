"""Offline tests for expanded runtime scope and credential isolation."""

import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = load_module("full_market_release", "infra/deploy/release_manifest.py")
loader = load_module("full_market_secrets", "infra/deploy/runtime-secrets/load_runtime_secrets.py")
policy = load_module("full_market_policy", "infra/deploy/change_policy.py")
STAGING_REGISTRY = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
DEPLOY = ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh"
GUARD = ROOT / "infra/deploy/check_full_market_checkpoint.sh"


class FullMarketDeploymentTests(unittest.TestCase):
    def manifest(self, target="production", unit="fetcher"):
        account = "289112218471" if target == "production" else "439622209937"
        registry = f"{account}.dkr.ecr.ap-southeast-1.amazonaws.com"
        manifest = {
            "schema_version": 2,
            "deployment_target": target,
            "unit": unit,
            "commit_sha": "a" * 40,
            "images": {
                name: f"{repository}@sha256:{'b' * 64}"
                for name, repository in release.image_repositories(target, unit, registry).items()
            },
            "migration_revision": "none" if unit == "fetcher" else "c" * 12,
            "contract_versions": [],
            "contract_manifest_sha256": "d" * 64,
            "deployment_source_bundle_sha256": "e" * 64,
            "created_by_run_id": "1234",
            "registry": registry,
            "account_id": account,
            "release_tag": f"{unit}-v1.0.0" if target == "production" else "",
        }
        if unit == "fetcher":
            manifest["fetcher_bundle_version"] = release.CURRENT_FETCHER_BUNDLE_VERSION
        if target == "production":
            manifest["promotion_source"] = {
                "accepted_bundle_key": f"{unit}/accepted/{'a' * 40}/{'b' * 64}.tar",
                "staging_registry": STAGING_REGISTRY,
            }
        return manifest

    def validate(self, manifest):
        contracts = release.ContractManifest(parsed={}, versions=(), canonical_sha256="d" * 64)
        release.validate_manifest(manifest, contracts)

    def test_manifests_without_a_runtime_profile_remain_valid(self):
        for target in ("staging", "production"):
            self.validate(self.manifest(target))

    def test_every_runtime_mounts_writable_shared_governor_with_full_disabled(self):
        source = (ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
        start = source.index("readiness_mount=()")
        end = source.index(
            '\nif [ "${FETCHER_CONSUMER_PROFILE:-pilot}" = full_market ]; then', start
        )
        mount_setup = source[start:end]
        for target in ("staging", "production"):
            for enabled in ("false", "true"):
                for retained in (False, True):
                    with self.subTest(target=target, enabled=enabled, retained=retained):
                        with tempfile.TemporaryDirectory() as directory:
                            base = Path(directory)
                            account_root = base / "account"
                            account_dir = account_root / target
                            readiness_dir = base / "readiness"
                            ledger = account_dir / "governor.sqlite3"
                            if retained:
                                account_dir.mkdir(parents=True)
                                ledger.write_bytes(b"retained pins, debt and cooldown")
                            # Execute the actual shell setup on isolated host paths.
                            # Only chown is recorded: this offline test is not root.
                            setup = mount_setup.replace(
                                "/var/lib/findb-account", str(account_root)
                            ).replace("/etc/findb-full-market/readiness", str(readiness_dir))
                            completed = subprocess.run(
                                [
                                    "bash",
                                    "-c",
                                    'set -euo pipefail\nsudo() { if [ "$1" = chown ]; then '
                                    'printf "%s\\n" "$*" >&2; else "$@"; fi; }\n'
                                    + setup
                                    + '\nprintf "%s\\n" ${account_mount[@]+"${account_mount[@]}"}',
                                ],
                                env={
                                    **os.environ,
                                    "APP_ENVIRONMENT": target,
                                    "FULL_MARKET_ENABLED": enabled,
                                },
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            self.assertEqual(completed.returncode, 0, completed.stderr)
                            args = completed.stdout.splitlines()
                            self.assertIn(f"type=bind,src={account_dir},dst={account_dir}", args)
                            self.assertIn(
                                f"type=bind,src={readiness_dir},dst={account_root}/readiness,readonly",
                                args,
                            )
                            self.assertEqual(account_dir.stat().st_mode & 0o777, 0o700)
                            self.assertIn(f"chown 10001:10001 {account_dir}", completed.stderr)
                            if retained:
                                self.assertEqual(
                                    ledger.read_bytes(), b"retained pins, debt and cooldown"
                                )
                            else:
                                self.assertFalse(ledger.exists())

    def test_legacy_runtime_profile_is_tolerated_only_for_fetcher_bundles(self):
        good = self.manifest()
        good["runtime_profile"] = "full-market"
        self.validate(good)
        staging = self.manifest("staging")
        staging["runtime_profile"] = "full-market"
        self.validate(staging)
        for target, unit in (("staging", "findb"), ("production", "findb")):
            wrong = self.manifest(target, unit)
            wrong["runtime_profile"] = "full-market"
            with self.assertRaises(release.ManifestError):
                self.validate(wrong)
        for invalid in (None, True, "FULL-MARKET", "full-market;echo unsafe"):
            wrong = copy.deepcopy(good)
            wrong["runtime_profile"] = invalid
            with self.assertRaises(release.ManifestError):
                self.validate(wrong)

    def test_profile_changes_immutable_bundle_manifest_identity(self):
        bounded = self.manifest()
        expanded = dict(bounded, runtime_profile="full-market")
        self.assertNotEqual(release.canonical_json(bounded), release.canonical_json(expanded))

    def test_materialized_fetcher_bundle_is_target_neutral(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            for target in ("staging", "production"):
                with self.subTest(target=target):
                    manifest = self.manifest(target)
                    with release.SourceBundleReader(ROOT) as sources:
                        contracts = sources.load_selected_contract_manifest(
                            ROOT / "contracts/manifest.json"
                        )
                        manifest["contract_versions"] = list(contracts.versions)
                        manifest["contract_manifest_sha256"] = contracts.canonical_sha256
                        manifest["deployment_source_bundle_sha256"] = (
                            sources.deployment_source_bundle_sha256("fetcher")
                        )
                    manifest_path = base / f"{target}.json"
                    manifest_path.write_bytes(release.canonical_json(manifest))
                    bundle = release.build_bundle(ROOT, manifest_path, "fetcher")
                    digest = hashlib.sha256(bundle).hexdigest()
                    output = base / f"{target}-release"
                    validated = release.materialize_validated_bundle(
                        bundle, expected_sha256=digest, unit="fetcher", output=output
                    )
                    self.assertNotIn("runtime_profile", validated)
                    self.assertFalse(
                        (output / "infra/deploy/inspect_full_market_installation.py").exists()
                    )
                    helper = (
                        output / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh"
                    ).read_text()
                    self.assertEqual(
                        subprocess.run(
                            [
                                "bash",
                                "-n",
                                str(output / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh"),
                            ],
                            capture_output=True,
                        ).returncode,
                        0,
                    )
                    self.assertNotIn("FETCHER_RUNTIME_PROFILE", helper)
                    self.assertNotIn("DEPLOYMENT_TARGET", helper)
                    self.assertIn('full_market_state="${FETCHER_FULL_MARKET_STATE_DIR:?', helper)

    def test_taifex_loads_no_provider_credentials(self):
        catalog = json.loads((ROOT / "infra/deploy/runtime-secrets/fetcher.json").read_text())
        fetched = []

        def fetch(name):
            fetched.append(name)
            relative = name.removeprefix("findb/production/fetcher/")
            declaration = next(
                secret
                for secret in catalog["consumers"]["taifex"]["secrets"]
                if secret["name"] == relative
            )
            return json.dumps({key: key + "-value" for key in declaration["required_keys"]})

        values = loader.load_consumer(catalog, "taifex", fetch, deployment_target="production")
        self.assertIn("FETCHER_TAIFEX_SOURCE_CLIENT_KEY", values)
        self.assertEqual(
            set(fetched),
            {
                "findb/production/fetcher/runtime/configuration",
                "findb/production/fetcher/api/calendar-serve",
                "findb/production/fetcher/api/source/taifex",
                "findb/production/fetcher/r2/raw",
            },
        )
        self.assertFalse(any("provider/" in name for name in fetched))
        self.assertFalse(
            {"TWELVE_DATA_API_KEY", "FINLAB_API_TOKEN", "SHIOAJI_API_KEY"} & values.keys()
        )

    def test_staging_full_market_still_requires_verified_release_before_host_mutation(self):
        environment = {
            **os.environ,
            "AWS_REGION": "ap-southeast-1",
            "AWS_ACCOUNT_ID": "439622209937",
            "APP_ENVIRONMENT": "staging",
            "ECR_REGISTRY": STAGING_REGISTRY,
            "FETCHER_RELEASE_ROOT": "/does-not-exist",
            "FETCHER_DEPLOY_MODE": "candidate",
            "TWELVE_IMAGE_REF": "unused",
            "FINLAB_IMAGE_REF": "unused",
            "SHIOAJI_IMAGE_REF": "unused",
        }
        completed = subprocess.run(
            ["bash", str(ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh")],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("release_root_invalid", completed.stderr)

    def test_taifex_provider_is_accepted_without_a_runtime_profile(self):
        completed = subprocess.run(
            [
                "bash",
                str(ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh"),
                "taifex",
                *(["unused"] * 8),
                "findb-fetch-full-market",
            ],
            env={
                **os.environ,
                "AWS_REGION": "ap-southeast-1",
                "AWS_ACCOUNT_ID": "439622209937",
                "APP_ENVIRONMENT": "staging",
                "ECR_REGISTRY": STAGING_REGISTRY,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        # No profile gate: the TAIFEX provider proceeds to the ordinary image contract.
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("taifex_profile_invalid", completed.stderr)
        self.assertIn("ecr_image_contract", completed.stderr)

    @unittest.skipUnless(shutil.which("jq"), "workflow replay guard requires jq")
    def test_replay_shim_takes_the_legacy_profile_from_the_bundled_manifest_only(self):
        workflow = (ROOT / ".github/workflows/fetcher-deploy.yml").read_text()
        start = workflow.rindex("\n", 0, workflow.index('legacy_profile="$(jq')) + 1
        end = workflow.index("\n          fi\n", start) + len("\n          fi\n")
        guard = "\n".join(line[10:] for line in workflow[start:end].splitlines())
        with tempfile.TemporaryDirectory() as directory:
            for manifest, mode, expected in (
                ({}, "candidate", ""),
                ({}, "replay", ""),
                ({"runtime_profile": "bounded"}, "replay", "bounded"),
                ({"runtime_profile": "full-market"}, "replay", "full-market"),
                ({"runtime_profile": "bounded"}, "candidate", None),
                ({"runtime_profile": "unsafe;profile"}, "replay", None),
            ):
                with self.subTest(manifest=manifest, mode=mode):
                    (Path(directory) / "release-manifest.json").write_text(json.dumps(manifest))
                    result = subprocess.run(
                        [
                            "bash",
                            "-c",
                            "set -euo pipefail\n" + guard + '\nprintf "%s" "$legacy_profile"',
                        ],
                        env={**os.environ, "RUNNER_TEMP": directory, "MODE": mode},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if expected is None:
                        self.assertNotEqual(result.returncode, 0)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, expected)
        ssm = workflow.split("- name: Run candidate acceptance or accepted replay", 1)[1]
        self.assertIn('if [ "$MODE" = replay ]; then', ssm)
        self.assertIn('runtime_exports="$runtime_exports FETCHER_RUNTIME_PROFILE=$q_profile"', ssm)
        self.assertLess(
            ssm.index('if [ "$MODE" = replay ]; then'),
            ssm.index("FETCHER_RUNTIME_PROFILE=$q_profile"),
        )
        self.assertNotIn("inputs.runtime_profile", workflow)

    def test_profile_transition_restores_retired_runtime_on_failure(self):
        source = (ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
        functions = []
        for name in (
            "register_provider",
            "registered_original",
            "require_retired_runtime",
            "retire_runtime",
            "register_named_full",
            "restore_named_identity",
            "rollback_named_full",
            "rollback_named_full_backups",
            "rollback_provider",
            "rollback_processed",
            "rollback_legacy_moves",
        ):
            match = re.search(rf"{name}\(\) \{{\n.*?\n\}}", source, re.DOTALL)
            self.assertIsNotNone(match)
            functions.append(match.group())
        # Docker is represented only in memory. Execute the actual shell
        # transaction functions without access to a daemon or any host paths.
        functions.insert(
            0,
            "runtime_inventory_recovery=none\n"
            + source[source.index("runtime_inventory_failure() {") : source.index("\nset +x")],
        )
        fake_docker = r"""
set -euo pipefail
container_rows=historical:true:old:accepted-image:true
processed=()
legacy_moves=()
named_full_originals=()
named_full_backups=()
rollback_failed=0
docker() {
  case "$1" in
    container)
      if [ "$2" = ls ]; then printf "%s\n" "$container_rows" | cut -d: -f1; else [ -n "$(printf '%s\n' "$container_rows" | awk -F: -v name="$3" '$1 == name {print $1}')" ]; fi ;;
    inspect)
      case "$3" in
        *State.Running*) printf '%s\n' "$container_rows" | awk -F: -v name="$4" '$1 == name {print $2}' ;;
        *State.Identity*) printf '%s\n' "$container_rows" | awk -F: -v name="$4" '$1 == name {print $3}' ;;
        *Config.Image*) printf '%s\n' "$container_rows" | awk -F: -v name="$4" '$1 == name {print $4}' ;;
        *Config.Labels*) printf '%s\n' "$container_rows" | awk -F: -v name="$4" '$1 == name {print $5}' ;;
        *State.ExitCode*) echo 0 ;;
        *State.OOMKilled*) echo false ;;
        *State.Error*) echo '' ;;
        *'{{.Id}}'*) printf '%s\n' "$container_rows" | awk -F: -v name="$4" '$1 == name {print $3}' ;;
        *) echo true ;;
      esac ;;
    stop) container_rows="$(printf '%s\n' "$container_rows" | awk -F: -v name="$4" 'BEGIN {OFS=":"} $1 == name {$2="false"} NF {print}')" ;;
    rm) container_rows="$(printf '%s\n' "$container_rows" | awk -F: -v name="$2" 'NF && $1 != name {print}')" ;;
    rename) container_rows="$(printf '%s\n' "$container_rows" | awk -F: -v name="$2" -v target="$3" 'BEGIN {OFS=":"} $1 == name {$1=target} NF {print}')" ;;
    start) container_rows="$(printf '%s\n' "$container_rows" | awk -F: -v name="$2" 'BEGIN {OFS=":"} $1 == name {$2="true"} NF {print}')" ;;
    *) return 99 ;;
  esac
}
"""
        scenario = r"""
retire_runtime historical
! docker container inspect historical
[ "$(docker inspect --format '{{.State.Running}}' historical-previous)" = false ]
container_rows="${container_rows}
historical:true:new:new-image:false"
rollback_processed
[ "$(docker inspect --format '{{.State.Running}}' historical)" = true ]
[ "$(docker inspect --format '{{.State.Identity}}' historical)" = old ]
[ "$(docker inspect --format '{{.Config.Image}}' historical)" = accepted-image ]
! docker container inspect historical-previous

"""
        completed = subprocess.run(
            ["bash", "-c", fake_docker + "\n".join(functions) + scenario],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_coordinator_installs_full_workers_in_every_target_without_a_profile(self):
        deploy = DEPLOY.read_text()
        self.assertNotIn("FETCHER_RUNTIME_PROFILE", deploy)
        self.assertNotIn("FETCHER_ACCEPTED_REPLAY", deploy)
        self.assertNotIn("DEPLOYMENT_TARGET", deploy)
        self.assertNotIn("legacy_historical_shutdown", deploy)
        full = deploy.index("export FETCHER_CONSUMER_PROFILE=full_market")
        loop = deploy.index("for provider in twelve-data finlab shioaji taifex; do", full)
        pilot = deploy.index("export FETCHER_CONSUMER_PROFILE=pilot", loop)
        self.assertLess(full, loop)
        self.assertLess(loop, pilot)
        self.assertNotIn("recorded_profile", deploy)
        self.assertIn('full_market_state="${FETCHER_FULL_MARKET_STATE_DIR:?', deploy)
        self.assertIn("full_market_state_dir_invalid", deploy)

    def test_checkpoint_gate_binds_state_dir_to_control_plane_and_legacy_formula(self):
        guard = GUARD.read_text()
        start = guard.index(': "${APP_ENVIRONMENT:?')
        end = guard.index("require_full_checkpoint() {", start)
        block = "set -euo pipefail\n" + guard[start:end] + '\nprintf "%s" "$full_market_state"'
        for target, state_dir, expected in (
            ("staging", "/var/lib/findb-full-market/staging", 0),
            ("production", "/var/lib/findb-full-market", 0),
            ("staging", "/var/lib/findb-full-market", "full_market_state_dir_mismatch"),
            (
                "production",
                "/var/lib/findb-full-market/production",
                "full_market_state_dir_mismatch",
            ),
            ("production", "/tmp/state", "full_market_state_dir_invalid"),
            ("staging", "/var/lib/findb-full-market/Staging", "full_market_state_dir_invalid"),
            ("local", "/var/lib/findb-full-market", "environment_invalid"),
            ("staging", None, "FETCHER_FULL_MARKET_STATE_DIR is required"),
        ):
            with self.subTest(target=target, state_dir=state_dir):
                env = {**os.environ, "APP_ENVIRONMENT": target}
                env.pop("FETCHER_FULL_MARKET_STATE_DIR", None)
                if state_dir is not None:
                    env["FETCHER_FULL_MARKET_STATE_DIR"] = state_dir
                result = subprocess.run(
                    ["bash", "-c", block], env=env, capture_output=True, text=True, check=False
                )
                if expected == 0:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, state_dir)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(expected, result.stderr)

    def test_infra_tests_run_ci_without_staging_deployment(self):
        routed = policy.classify(["infra/tests/test_full_market_deployment.py"])
        self.assertTrue(routed["findb_ci"])
        self.assertFalse(routed["findb_staging"])
        self.assertFalse(routed["fetcher_staging"])


if __name__ == "__main__":
    unittest.main()
