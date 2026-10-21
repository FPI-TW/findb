"""Offline tests for expanded runtime scope and credential isolation."""

import copy
import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from script_env import script_env

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
    def test_image_preparation_never_loads_provider_or_application_secrets(self):
        catalog = json.loads((ROOT / "infra/deploy/runtime-secrets/fetcher.json").read_text())

        def forbidden_fetch(_name):
            self.fail("image preparation must not fetch credentials")

        self.assertEqual(
            loader.load_consumer(
                catalog, "images", forbidden_fetch, deployment_target="production"
            ),
            {},
        )

    def manifest(self, target="production", unit="fetcher"):
        account = "289112218471" if target == "production" else "439622209937"
        registry = f"{account}.dkr.ecr.ap-southeast-1.amazonaws.com"
        manifest = {
            "schema_version": 3,
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
            manifest.update(runtime_profile="unified", fetcher_bundle_version=2)
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

    def test_every_runtime_mounts_writable_shared_governor_with_full_disabled(self):
        source = (ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
        start = source.index("readiness_mount=()")
        end = source.index("\n{\n  # The official", start)
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
                                    **script_env(),
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

    def test_full_market_is_environment_bound_fetcher_only(self):
        good = self.manifest()
        good["runtime_profile"] = "unified"
        self.validate(good)
        staging = self.manifest("staging")
        staging["runtime_profile"] = "unified"
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
                ("staging", "unified"),
                ("production", "unified"),
            ):
                with self.subTest(target=target, profile=profile):
                    manifest = self.manifest(target)
                    manifest["fetcher_bundle_version"] = 2
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
                        "bounded",
                    ):
                        completed = subprocess.run(
                            ["bash", "-c", preflight],
                            env={
                                **script_env(),
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
                "findb/production/fetcher/api/source/taifex",
                "findb/production/fetcher/r2/raw",
            },
        )
        self.assertFalse(any("provider/" in name for name in fetched))
        self.assertFalse(
            {"TWELVE_DATA_API_KEY", "FINLAB_API_TOKEN", "SHIOAJI_API_KEY"} & values.keys()
        )

    def test_factset_credentials_are_isolated_to_its_consumers(self):
        catalog = json.loads((ROOT / "infra/deploy/runtime-secrets/fetcher.json").read_text())
        fetched = []

        def fetch(name):
            fetched.append(name)
            relative = name.removeprefix("findb/staging/fetcher/")
            declaration = next(
                secret
                for secret in catalog["consumers"]["factset"]["secrets"]
                if secret["name"] == relative
            )
            return json.dumps({key: key + "-value" for key in declaration["required_keys"]})

        values = loader.load_consumer(catalog, "factset", fetch, deployment_target="staging")
        self.assertEqual(
            set(fetched),
            {
                "findb/staging/fetcher/runtime/configuration",
                "findb/staging/fetcher/api/source/factset",
                "findb/staging/fetcher/provider/factset",
                "findb/staging/fetcher/r2/raw",
            },
        )
        self.assertTrue(
            {
                "FACTSET_HOST",
                "FACTSET_USER",
                "FACTSET_PASSWORD",
                "FETCHER_FACTSET_SOURCE_CLIENT_KEY",
            }
            <= values.keys()
        )
        self.assertFalse(
            {"TWELVE_DATA_API_KEY", "FINLAB_API_TOKEN", "SHIOAJI_API_KEY"} & values.keys()
        )
        for name, consumer in catalog["consumers"].items():
            if name != "factset":
                self.assertFalse(
                    any(secret["name"] == "provider/factset" for secret in consumer["secrets"])
                )

    def test_staging_full_market_still_requires_verified_release_before_host_mutation(self):
        environment = {
            **script_env(),
            "AWS_REGION": "ap-southeast-1",
            "AWS_ACCOUNT_ID": "439622209937",
            "APP_ENVIRONMENT": "staging",
            "ECR_REGISTRY": release.REGISTRY,
            "FETCHER_RELEASE_ROOT": "/does-not-exist",
            "FETCHER_DEPLOY_MODE": "candidate",
            "TWELVE_IMAGE_REF": "unused",
            "FINLAB_IMAGE_REF": "unused",
            "SHIOAJI_IMAGE_REF": "unused",
            "FACTSET_IMAGE_REF": "unused",
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

    def test_fixed_topology_installs_both_profiles_with_full_flag_off(self):
        source = (ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
        self.assertIn("for execution_profile in pilot full_market; do", source)
        self.assertIn("for provider in twelve-data finlab shioaji taifex factset; do", source)
        self.assertIn('findb-fetch-runtime --execution-profile "$execution_profile"', source)
        self.assertNotIn('[ "$FULL_MARKET_ENABLED" =', source)

    def test_rollback_floor_rejects_legacy_runtime_profiles(self):
        for profile in (None, "bounded", "full-market"):
            manifest = self.manifest()
            manifest["runtime_profile"] = profile
            with self.assertRaises(release.ManifestError):
                self.validate(manifest)

    def test_first_cutover_rejects_legacy_containers(self):
        source = (ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
        self.assertIn("reason=legacy_runtime_requires_cutover", source)

    def test_infra_tests_run_ci_without_staging_deployment(self):
        routed = policy.classify(["infra/tests/test_full_market_deployment.py"])
        self.assertTrue(routed["findb_ci"])
        self.assertFalse(routed["findb_staging"])
        self.assertFalse(routed["fetcher_staging"])


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
                "runtime_profile": "unified",
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
                    "consumers": ["full_market", "pilot", "historical", "maintenance"],
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
                consumer = (
                    "historical"
                    if "historical" in name
                    else "full_market"
                    if "full-market" in name
                    else "pilot"
                )
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
            self.assertEqual(len(calls), 8)  # Both allocations plus actual config, twice.
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


if __name__ == "__main__":
    unittest.main()
