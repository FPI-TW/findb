"""The HKEX daily consumer reads the Owner's HSIL archive only while it is on the host."""

import subprocess
from pathlib import Path

import pytest
from script_env import script_env

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST_DIR = "/var/lib/findb-hsil-archive"


def _archive_setup(archive: Path) -> str:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    start = helper.index("# HSIL reports HSIL no longer serves")
    end = helper.index('\nif [ "$accepted_release" = false ]; then', start)
    block = helper[start:end]
    assert block.count(HOST_DIR) == 1
    return block.replace(HOST_DIR, str(archive))


def _arguments(archive: Path, provider: str, profile: str) -> subprocess.CompletedProcess:
    shell = "\n".join(
        [
            "set -euo pipefail",
            f"provider={provider}",
            "common_args=(--read-only)",
            _archive_setup(archive),
            'printf "%s\\n" "${common_args[@]}"',
        ]
    )
    return subprocess.run(
        ["/bin/bash", "-c", shell],
        env={**script_env(), "FETCHER_CONSUMER_PROFILE": profile},
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_present_archive_is_mounted_read_only_for_the_hkex_daily_consumer(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "manifest.json").write_text("{}")

    completed = _arguments(archive, "hkex", "full_market")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "--read-only",
        "--mount",
        f"type=bind,src={archive},dst={archive},readonly",
        "--env",
        f"HSIL_DOCUMENT_ARCHIVE_DIR={archive}",
    ]


@pytest.mark.parametrize(
    ("provider", "profile", "manifest"),
    [
        ("hkex", "full_market", False),
        ("hkex", "historical", True),
        ("twse", "full_market", True),
    ],
)
def test_no_archive_or_another_consumer_changes_nothing(tmp_path, provider, profile, manifest):
    archive = tmp_path / "archive"
    archive.mkdir()
    if manifest:
        (archive / "manifest.json").write_text("{}")

    completed = _arguments(archive, provider, profile)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["--read-only"]


def test_an_archive_behind_a_symlink_fails_the_release(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "manifest.json").write_text("{}")
    link = tmp_path / "archive"
    link.symlink_to(real)

    completed = _arguments(link, "hkex", "full_market")

    assert completed.returncode == 1
    assert "reason=hsil_archive_unsafe" in completed.stderr
