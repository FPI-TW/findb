"""Offline tests for the committed control plane and zero-variable GitHub Environments."""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_ROOT = ROOT / "infra" / "env"
WORKFLOWS = ROOT / ".github" / "workflows"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control_plane = load_module("control_plane_under_test", "infra/env/control_plane.py")

TRUSTED_LOAD_STEP = "control_plane.py export --target"
TRUSTED_CHECKOUT_REF = "ref: ${{ github.workflow_sha }}"


class ControlPlaneConfigurationTests(unittest.TestCase):
    def test_committed_files_validate_and_share_one_key_set(self):
        loaded = control_plane.validate_all(ENV_ROOT)
        self.assertEqual(
            set(loaded), {(t, u) for t in control_plane.TARGETS for u in control_plane.UNITS}
        )
        for unit in control_plane.UNITS:
            staging_keys = sorted(loaded[("staging", unit)])
            production_keys = sorted(loaded[("production", unit)])
            self.assertEqual(staging_keys, production_keys)
            self.assertEqual(staging_keys, sorted(control_plane.keys_for(unit)))
        self.assertNotEqual(
            loaded[("staging", "findb")]["AWS_ACCOUNT_ID"],
            loaded[("production", "findb")]["AWS_ACCOUNT_ID"],
        )
        self.assertEqual(
            loaded[("production", "fetcher")]["STAGING_DEPLOY_BUNDLE_BUCKET"],
            loaded[("staging", "fetcher")]["DEPLOY_BUNDLE_BUCKET"],
        )
        self.assertEqual(
            loaded[("staging", "fetcher")]["FETCHER_FULL_MARKET_STATE_DIR"],
            "/var/lib/findb-full-market/staging",
        )
        self.assertEqual(
            loaded[("production", "fetcher")]["FETCHER_FULL_MARKET_STATE_DIR"],
            "/var/lib/findb-full-market",
        )

    def test_export_emits_sorted_github_env_lines_without_control_characters(self):
        values = control_plane.load("staging", "findb", ENV_ROOT)
        lines = control_plane.export_lines(values)
        self.assertEqual(lines, sorted(lines))
        for line in lines:
            self.assertRegex(line, r"^[A-Z][A-Z0-9_]*=[^\r\n]*$")
        completed = subprocess.run(
            [
                sys.executable,
                str(ENV_ROOT / "control_plane.py"),
                "export",
                "--target",
                "production",
                "--unit",
                "fetcher",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        exported = dict(line.split("=", 1) for line in completed.stdout.splitlines())
        self.assertEqual(exported["AWS_PUBLISHER_ROLE_ARN"], "")
        self.assertEqual(
            exported["AWS_PROMOTION_ROLE_ARN"],
            "arn:aws:iam::289112218471:role/fetcher-production-promotion",
        )
        self.assertEqual(exported["FINDB_PUBLIC_HOST"], "findb.tingfong.com")

    def _corrupted_copy(self, target, unit, mutate):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        for t in control_plane.TARGETS:
            (work / t).mkdir()
            for u in control_plane.UNITS:
                shutil.copy(ENV_ROOT / t / f"{u}.json", work / t / f"{u}.json")
        path = work / target / f"{unit}.json"
        document = json.loads(path.read_text())
        mutate(document)
        path.write_text(json.dumps(document))
        return work

    def test_validation_fails_closed_on_cross_target_and_emptiness_rules(self):
        cases = [
            (
                "staging",
                "findb",
                lambda d: d["variables"].__setitem__(
                    "AWS_PROMOTION_ROLE_ARN",
                    "arn:aws:iam::439622209937:role/findb-production-promotion",
                ),
            ),
            (
                "production",
                "findb",
                lambda d: d["variables"].__setitem__(
                    "AWS_PUBLISHER_ROLE_ARN",
                    "arn:aws:iam::289112218471:role/findb-staging-ecr-publisher",
                ),
            ),
            (
                "production",
                "fetcher",
                lambda d: d["variables"].__setitem__("STAGING_AWS_ACCOUNT_ID", "289112218471"),
            ),
            (
                "staging",
                "fetcher",
                lambda d: d["variables"].__setitem__("FULL_MARKET_ENABLED", "false"),
            ),
            (
                "staging",
                "fetcher",
                lambda d: d["variables"].__setitem__("FETCHER_FULL_MARKET_STATE_DIR", "/tmp/state"),
            ),
            (
                "staging",
                "findb",
                lambda d: d["variables"].__setitem__("FINDB_PUBLIC_HOST", "Findb.Example.com\n"),
            ),
            ("staging", "findb", lambda d: d["variables"].pop("RDS_DB_INSTANCE_IDENTIFIER")),
            (
                "staging",
                "findb",
                lambda d: d["variables"].__setitem__("DEPLOYMENT_TARGET", "staging"),
            ),
            ("production", "findb", lambda d: d.__setitem__("deployment_target", "staging")),
        ]
        for target, unit, mutate in cases:
            with self.subTest(target=target, unit=unit):
                work = self._corrupted_copy(target, unit, mutate)
                with self.assertRaises(control_plane.ControlPlaneError):
                    control_plane.validate_all(work)

    def test_workflows_read_no_github_environment_variables_or_secrets(self):
        allowed_repository_variable = {"staging-infra-plan.yml": {"STAGING_INFRA_PLAN_ROLE_ARN"}}
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            text = workflow.read_text()
            self.assertNotIn(
                "secrets.",
                text.replace("load_runtime_secrets.py", "").replace("runtime-secrets/", ""),
                workflow.name,
            )
            found = set(re.findall(r"\$\{\{\s*vars\.([A-Z0-9_]+)", text))
            self.assertEqual(
                found, allowed_repository_variable.get(workflow.name, set()), workflow.name
            )
            self.assertNotIn("PRODUCTION_DEPLOY_ENABLED", text, workflow.name)
            self.assertNotIn("STAGING_ECR_CUTOVER_ENABLED", text, workflow.name)

    def test_every_control_plane_consumer_loads_it_from_the_workflow_sha(self):
        for workflow in (
            "cd.yml",
            "findb-deploy.yml",
            "fetcher-deploy.yml",
            "fetcher-finlab-smoke.yml",
        ):
            text = (WORKFLOWS / workflow).read_text()
            self.assertIn(TRUSTED_LOAD_STEP, text, workflow)
            self.assertIn(TRUSTED_CHECKOUT_REF, text, workflow)
            for path in (
                "infra/env/control_plane.py",
                "infra/env/staging/findb.json",
                "infra/env/staging/fetcher.json",
                "infra/env/production/findb.json",
                "infra/env/production/fetcher.json",
            ):
                self.assertIn(path, text, (workflow, path))
            # Nothing hardcodes the staging account; the control plane supplies it.
            self.assertNotIn("439622209937", text, workflow)
            self.assertNotIn("289112218471", text, workflow)

    def test_retired_github_environment_sync_is_gone(self):
        self.assertFalse((ENV_ROOT / "sync_github_environment.py").exists())
        for target in control_plane.TARGETS:
            for unit in control_plane.UNITS:
                self.assertFalse((ENV_ROOT / target / unit / "remote.env.example").exists())
        package = json.loads((ROOT / "package.json").read_text())
        self.assertIn("../infra/env/control_plane.py", package["scripts"]["check:infra"])
        self.assertNotIn("sync_github_environment", package["scripts"]["check:infra"])


if __name__ == "__main__":
    unittest.main()
