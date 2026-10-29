"""Deployment rejects the retired mode before Docker, AWS, or filesystem work."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("unit", ["findb", "fetcher"])
def test_legacy_mode_rejected_before_deployment_io(unit, tmp_path):
    script = ROOT / f"infra/deploy/runtime-secrets/deploy_{unit}_aws.sh"
    result = subprocess.run(
        ["/bin/bash", str(script)],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "PROVIDER_WORK_REQUIRED": "false"},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "reason=legacy_provider_execution_retired" in result.stderr
    assert result.stdout == ""
    assert not list(tmp_path.iterdir())
