"""Docker progress must not crowd the terminal marker out of SSM stdout."""

import os
import subprocess
from pathlib import Path

import pytest
from script_env import script_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh"


@pytest.mark.parametrize(
    ("prepared", "failure"), [(False, ""), (False, "pull"), (True, ""), (True, "image")]
)
def test_docker_progress_preserves_bounded_terminal_protocol(tmp_path, prepared, failure):
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "print('progress ' * 10000)\n"
        "if sys.argv[1] == 'image': print(os.environ['image'])\n"
        "if sys.argv[1] == os.environ['DOCKER_FAILURE']:\n"
        "    print('docker operation failed', file=sys.stderr)\n"
        "    sys.exit(17)\n"
    )
    docker.chmod(0o755)
    source = SCRIPT.read_text()
    start = source.index('if [ "${FETCHER_IMAGES_PREPARED:-false}" = true ]; then')
    end = source.index("cache_mount=()", start)
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", source[start:end] + "echo terminal_success"],
        env={
            **script_env(),
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "DOCKER_FAILURE": failure,
            "FETCHER_IMAGES_PREPARED": str(prepared).lower(),
            "image": "registry/image@sha256:" + "a" * 64,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert len(result.stdout.encode()) < 24000
    if failure:
        assert result.returncode == 17
        assert "terminal_success" not in result.stdout
        assert "docker operation failed" in result.stderr
    else:
        assert result.returncode == 0
        assert result.stdout == "terminal_success\n"
