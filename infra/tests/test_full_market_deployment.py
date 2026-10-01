"""Offline tests for expanded runtime scope and credential isolation."""

import copy
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

    def test_full_market_is_production_fetcher_only(self):
        good = self.manifest()
        good["runtime_profile"] = "full-market"
        self.validate(good)
        for target, unit in (("staging", "fetcher"), ("production", "findb")):
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

    def test_full_market_rejected_on_staging_before_host_mutation(self):
        environment = {
            **os.environ,
            "AWS_REGION": "ap-southeast-1",
            "AWS_ACCOUNT_ID": "439622209937",
            "DEPLOYMENT_TARGET": "staging",
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
        self.assertIn("runtime_profile_invalid", completed.stderr)

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
    def test_replay_guard_binds_record_profile_and_legacy_default(self):
        workflow = (ROOT / ".github/workflows/fetcher-deploy.yml").read_text()
        guard = next(
            line.strip()
            for line in workflow.splitlines()
            if line.strip().startswith("[ ")
            and "runtime_profile //" in line
            and '"$RUNNER_TEMP/acceptance.json"' in line
        )
        with tempfile.TemporaryDirectory() as directory:
            for record, requested, accepted in (
                ({}, "bounded", True),
                ({}, "full-market", False),
                ({"runtime_profile": "bounded"}, "full-market", False),
                ({"runtime_profile": "full-market"}, "bounded", False),
                ({"runtime_profile": "full-market"}, "full-market", True),
            ):
                (Path(directory) / "acceptance.json").write_text(json.dumps(record))
                completed = subprocess.run(
                    ["bash", "-c", guard],
                    env={
                        **os.environ,
                        "RUNNER_TEMP": directory,
                        "FETCHER_RUNTIME_PROFILE": requested,
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode == 0, accepted)

    def test_profile_transition_restores_retired_runtime_on_failure(self):
        source = (ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
        functions = []
        for name in (
            "register_provider",
            "retire_runtime",
            "rollback_provider",
            "rollback_processed",
        ):
            match = re.search(rf"{name}\(\) \{{\n.*?\n\}}", source, re.DOTALL)
            self.assertIsNotNone(match)
            functions.append(match.group())
        # Docker is represented only in memory. Execute the actual shell
        # transaction functions without access to a daemon or any host paths.
        fake_docker = r"""
set -euo pipefail
container_rows=historical:true:old
processed=()
rollback_failed=0
docker() {
  case "$1" in
    container)
      [ -n "$(printf '%s\n' "$container_rows" | awk -F: -v name="$3" '$1 == name {print $1}')" ] ;;
    inspect)
      case "$3" in
        *State.Running*) printf '%s\n' "$container_rows" | awk -F: -v name="$4" '$1 == name {print $2}' ;;
        *State.Identity*) printf '%s\n' "$container_rows" | awk -F: -v name="$4" '$1 == name {print $3}' ;;
        *State.ExitCode*) echo 0 ;;
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
historical:true:new"
rollback_processed
[ "$(docker inspect --format '{{.State.Running}}' historical)" = true ]
[ "$(docker inspect --format '{{.State.Identity}}' historical)" = old ]
! docker container inspect historical-previous

"""
        completed = subprocess.run(
            ["bash", "-c", fake_docker + "\n".join(functions) + scenario],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_infra_tests_run_ci_without_staging_deployment(self):
        routed = policy.classify(["infra/tests/test_full_market_deployment.py"])
        self.assertTrue(routed["findb_ci"])
        self.assertFalse(routed["findb_staging"])
        self.assertFalse(routed["fetcher_staging"])


if __name__ == "__main__":
    unittest.main()
