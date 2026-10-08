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
        if target == "production":
            manifest["promotion_source"] = {
                "accepted_bundle_key": f"{unit}/accepted/{'a' * 40}/{'b' * 64}.tar",
                "staging_registry": release.REGISTRY,
            }
        return manifest

    def validate(self, manifest):
        contracts = release.ContractManifest(parsed={}, versions=(), canonical_sha256="d" * 64)
        release.validate_manifest(manifest, contracts)

    def test_legacy_bounded_remains_valid(self):
        for target in ("staging", "production"):
            self.validate(self.manifest(target))

    def test_full_market_is_environment_bound_fetcher_only(self):
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

    def test_materialized_fetcher_bundle_passes_actual_profile_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            for target, profile in (
                ("staging", "bounded"),
                ("production", "full-market"),
            ):
                with self.subTest(target=target, profile=profile):
                    manifest = self.manifest(target)
                    manifest["fetcher_bundle_version"] = 1
                    if profile == "full-market":
                        manifest["runtime_profile"] = profile
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
                    expected_inputs = {
                        "unit": "fetcher",
                        "commit_sha": manifest["commit_sha"],
                        "migration_revision": "none",
                        "created_by_run_id": manifest["created_by_run_id"],
                        "images": manifest["images"],
                        "deployment_target": target,
                    }
                    release.validate_bundle_bytes(
                        bundle,
                        expected_sha256=digest,
                        unit="fetcher",
                        expected_inputs=expected_inputs,
                    )
                    output = base / f"{target}-release"
                    release.materialize_validated_bundle(
                        bundle,
                        expected_sha256=digest,
                        unit="fetcher",
                        output=output,
                        expected_inputs=expected_inputs,
                    )
                    # Run the exact host profile preflight from the bundled
                    # deployment helper. Stop before image/provider/secret or
                    # container work, without creating a host /opt release.
                    source = (
                        output / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh"
                    ).read_text()
                    start = source.index('manifest_profile="$(python3 ')
                    end = source.index("\nvalidate_image()", start)
                    preflight = "set -euo pipefail\n" + source[start:end]
                    for requested in (
                        profile,
                        "full-market" if profile == "bounded" else "bounded",
                    ):
                        completed = subprocess.run(
                            ["bash", "-c", preflight],
                            env={
                                **os.environ,
                                "FETCHER_RELEASE_ROOT": str(output),
                                "FETCHER_RUNTIME_PROFILE": requested,
                            },
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertEqual(
                            completed.returncode == 0,
                            requested == profile,
                            completed.stderr,
                        )
                        if requested != profile:
                            self.assertIn("runtime_profile_manifest_mismatch", completed.stderr)

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
            "ECR_REGISTRY": release.REGISTRY,
            "FETCHER_RELEASE_ROOT": "/does-not-exist",
            "FETCHER_DEPLOY_MODE": "candidate",
            "TWELVE_IMAGE_REF": "unused",
            "FINLAB_IMAGE_REF": "unused",
            "SHIOAJI_IMAGE_REF": "unused",
            "FETCHER_RUNTIME_PROFILE": "full-market",
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

    def test_taifex_rejected_outside_expanded_profile_before_secret_use(self):
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
                "FETCHER_RUNTIME_PROFILE": "bounded",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("taifex_profile_invalid", completed.stderr)

    @unittest.skipUnless(shutil.which("jq"), "workflow replay guard requires jq")
    def test_replay_uses_record_profile_and_legacy_default(self):
        workflow = (ROOT / ".github/workflows/fetcher-deploy.yml").read_text()
        guard = next(
            line.strip()
            for line in workflow.splitlines()
            if line.strip().startswith("FETCHER_RUNTIME_PROFILE=") and "runtime_profile //" in line
        )
        with tempfile.TemporaryDirectory() as directory:
            for record, expected in (
                ({}, "bounded"),
                ({"runtime_profile": "bounded"}, "bounded"),
                ({"runtime_profile": "full-market"}, "full-market"),
            ):
                (Path(directory) / "acceptance.json").write_text(json.dumps(record))
                result = subprocess.run(
                    ["bash", "-c", guard + '\nprintf "%s" "$FETCHER_RUNTIME_PROFILE"'],
                    env={**os.environ, "RUNNER_TEMP": directory},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.stdout, expected)

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

    def test_bounded_accepted_replay_preserves_compatible_installed_drainer_image(self):
        source = (ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
        start = source.index(
            '    if [ "$recorded_profile" = bounded ] && [ "${FETCHER_ACCEPTED_REPLAY:-false}" = true ]; then'
        )
        end = source.index("\n    fi", start) + len("\n    fi")
        guard = source[start:end]
        registration = "\n".join(
            re.search(rf"{name}\(\) \{{\n.*?\n\}}", source, re.S).group()
            for name in ("register_named_full", "register_provider", "registered_original")
        )
        inventory = (
            "runtime_inventory_recovery=none\n"
            + source[source.index("runtime_inventory_failure() {") : source.index("\nset +x")]
        )
        fake = r"""
set -euo pipefail
processed=(); named_full_originals=(); named_full_backups=()
stable=drainer
# Every row binds a distinct name, running state, ID, image and accepted label.
case "$layout" in
  stable) container_rows=drainer:false:accepted-id:installed-compatible:true ;;
  previous-only) container_rows=drainer-previous:false:accepted-id:installed-compatible:true ;;
  interrupted-stable) container_rows='drainer:false:interrupted-id:interrupted-image:false
 drainer-previous:false:accepted-id:installed-compatible:true'
    container_rows="${container_rows// drainer/drainer}" ;;
  *) exit 99 ;;
esac
docker() {
  local row="$(printf '%s\n' "$container_rows" | awk -F: -v name="${!#}" '$1 == name {print}')"
  case "$1" in
    container)
      if [ "$2" = ls ]; then printf '%s\n' "$container_rows" | cut -d: -f1; else [ -n "$row" ]; fi ;;
    inspect)
      [ -n "$row" ] || return 1
      case "$3" in
        *State.Running*) printf '%s\n' "$row" | cut -d: -f2 ;;
        *'{{.Id}}'*) printf '%s\n' "$row" | cut -d: -f3 ;;
        *Config.Image*) printf '%s\n' "$row" | cut -d: -f4 ;;
        *Config.Labels*) printf '%s\n' "$row" | cut -d: -f5 ;;
        *) return 97 ;;
      esac ;;
    *) return 96 ;;
  esac
}
"""
        for layout in ("stable", "previous-only", "interrupted-stable"):
            for profile, replay, expected in (
                ("bounded", "true", "installed-compatible"),
                ("bounded", "false", "bundle-image"),
                ("full-market", "true", "bundle-image"),
            ):
                with self.subTest(layout=layout, profile=profile, replay=replay):
                    script = (
                        fake
                        + inventory
                        + registration
                        + '\nregister_provider "$stable" "$stable-previous"\nimage=bundle-image\n'
                        + guard
                        + '\nprintf "%s" "$image"'
                    )
                    result = subprocess.run(
                        ["bash", "-c", script],
                        env={
                            **os.environ,
                            "recorded_profile": profile,
                            "FETCHER_ACCEPTED_REPLAY": replay,
                            "layout": layout,
                        },
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    self.assertEqual(result.stdout, expected)
        workflow = (ROOT / ".github/workflows/fetcher-deploy.yml").read_text()
        self.assertIn(
            '[ "$MODE" != replay ] || runtime_exports="$runtime_exports FETCHER_ACCEPTED_REPLAY=true"',
            workflow,
        )

    def test_infra_tests_run_ci_without_staging_deployment(self):
        routed = policy.classify(["infra/tests/test_full_market_deployment.py"])
        self.assertTrue(routed["findb_ci"])
        self.assertFalse(routed["findb_staging"])
        self.assertFalse(routed["fetcher_staging"])


if __name__ == "__main__":
    unittest.main()


class InstallationReceiptTests(unittest.TestCase):
    def test_receipt_inspects_running_image_config_source_and_every_allocation(self):
        from unittest.mock import patch

        inspection = load_module(
            "full_market_installation", "infra/deploy/inspect_full_market_installation.py"
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest = {
                "deployment_target": "staging",
                "runtime_profile": "full-market",
                "unit": "fetcher",
                "commit_sha": "a" * 40,
                "images": {"finlab": "image@sha256:" + "b" * 64},
            }
            accepted = {**manifest, "state": "accepted", "bundle_sha256": "c" * 64}
            config = {"deployment_target": "staging", "desired_state": "stopped"}
            allocation = {
                "provider": "finlab",
                "environment": "staging",
                "requests_per_second": 3,
                "source_requests_per_minute": 120,
                "account": {
                    "governor_identity": "/account/governor.sqlite3",
                    "consumers": ["full_market", "pilot", "maintenance"],
                },
            }
            for name, value in (
                ("manifest", manifest),
                ("accepted", accepted),
                ("config", config),
                ("allocation", allocation),
            ):
                (base / name).write_text(json.dumps(value))
            normalized = {**allocation, "requests_per_second": 3.0}
            allocation_sha = hashlib.sha256(
                json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()

            environment_fields = ["APP_ENVIRONMENT=staging"]

            def row(name):
                consumer = "full_market" if "full-market" in name else "pilot"
                return {
                    "Id": consumer + "-container",
                    "Config": {
                        "Image": manifest["images"]["finlab"],
                        "Labels": {"com.findb.fetcher.accepted": "true"},
                        "Cmd": ["findb-fetch-full-market", "--config", "/installed/config"],
                        "Env": [
                            *environment_fields,
                            "FULL_MARKET_ENABLED=true",
                            "FETCHER_ACCOUNT_STATE_PATH=/account/governor.sqlite3",
                            "FETCHER_CONSUMER_PROFILE=" + consumer,
                            "SOURCE_CLIENT_KEY=private-key",
                            "FETCHER_ACCOUNT_READINESS_FILE=/readiness/finlab.json",
                        ],
                    },
                    "Mounts": [{"Destination": "/account", "RW": True}],
                    "State": {"Running": True},
                }

            calls = []

            def output(command, **kwargs):
                calls.append(command)
                return (
                    inspection.digest(base / "config")
                    if command[-1] == "/installed/config"
                    else allocation_sha
                ) + "\n"

            with (
                patch.object(inspection, "inspect", side_effect=row),
                patch.object(inspection.subprocess, "check_output", side_effect=output),
            ):
                receipt = inspection.build(
                    base / "manifest",
                    base / "accepted",
                    base / "config",
                    "finlab",
                    "/account/governor.sqlite3",
                    base / "allocation",
                )
                restarted = inspection.build(
                    base / "manifest",
                    base / "accepted",
                    base / "config",
                    "finlab",
                    "/account/governor.sqlite3",
                    base / "allocation",
                )
            self.assertEqual(receipt["runtime_id"], restarted["runtime_id"])
            self.assertEqual(receipt["account_allocation_sha256"], allocation_sha)
            self.assertEqual(
                receipt["source_key_sha256"], hashlib.sha256(b"private-key").hexdigest()
            )
            self.assertNotIn("private-key", json.dumps(receipt))
            self.assertEqual(len(calls), 6)  # Both allocations plus actual config, twice.
            environment_fields[:] = ["DEPLOYMENT_TARGET=staging"]
            with (
                patch.object(inspection, "inspect", side_effect=row),
                patch.object(inspection.subprocess, "check_output", side_effect=output),
            ):
                arguments = (
                    base / "manifest",
                    base / "accepted",
                    base / "config",
                    "finlab",
                    "/account/governor.sqlite3",
                    base / "allocation",
                )
                with self.assertRaisesRegex(ValueError, "explicit legacy inspection"):
                    inspection.build(*arguments)
                legacy = inspection.build(*arguments, allow_legacy_environment=True)
                self.assertEqual(legacy["environment_contracts"], ["legacy-deployment-target-v1"])
                environment_fields[:] = ["APP_ENVIRONMENT=staging", "DEPLOYMENT_TARGET=production"]
                with self.assertRaisesRegex(ValueError, "metadata conflict"):
                    inspection.build(*arguments, allow_legacy_environment=True)
            environment_fields[:] = ["APP_ENVIRONMENT=staging"]
            with (
                patch.object(inspection, "inspect", side_effect=row),
                patch.object(inspection.subprocess, "check_output", return_value="changed\n"),
            ):
                with self.assertRaisesRegex(ValueError, "running consumer allocation"):
                    inspection.build(
                        base / "manifest",
                        base / "accepted",
                        base / "config",
                        "finlab",
                        "/account/governor.sqlite3",
                        base / "allocation",
                    )
            bad = row("findb-full-market-finlab")
            bad["Config"]["Image"] = "wrong-image"
            with patch.object(inspection, "inspect", return_value=bad):
                with self.assertRaisesRegex(ValueError, "image/environment/governor"):
                    inspection.build(
                        base / "manifest",
                        base / "accepted",
                        base / "config",
                        "finlab",
                        "/account/governor.sqlite3",
                        base / "allocation",
                    )
