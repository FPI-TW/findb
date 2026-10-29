"""Execute the provider images' actual default command without overriding it."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("provider", ["finlab", "shioaji", "factset"])
def test_provider_image_default_command_prints_help_without_credentials(provider, tmp_path):
    dockerfile = Path(__file__).resolve().parents[1] / f"Dockerfile.{provider}"
    command = json.loads(
        next(line[4:] for line in dockerfile.read_text().splitlines() if line.startswith("CMD "))
    )
    assert shutil.which(command[0]) is not None
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--execution-profile" in result.stdout and "--provider" in result.stdout
    assert not list(tmp_path.iterdir())
