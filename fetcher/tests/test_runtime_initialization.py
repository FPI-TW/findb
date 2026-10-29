"""Offline checkpoint bootstrap must not compete with active provider work."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from findb_fetcher import provider_work

CONFIG = Path(__file__).resolve().parents[1] / "configs/full_market.local.v1.json"


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex", "factset"])
@pytest.mark.parametrize("profile", ["pilot", "full_market"])
def test_checkpoint_bootstrap_preserves_live_owner_and_shared_inode(
    tmp_path, monkeypatch, provider, profile
):
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    account = tmp_path / "account"
    account.mkdir()
    governor = account / "governor.sqlite3"
    governor.write_bytes(b"retained account budgets and cooldown")
    active_checkpoint = tmp_path / "active-checkpoint.sqlite3"
    active_checkpoint.write_bytes(b"retained prepared bodies and receipts")
    checkpoint = tmp_path / profile / "state.sqlite3"
    with provider_work.host_lock(provider, root=account) as (fd, identity):
        os.write(fd, b"existing-lock-evidence")
        lock = account / f"provider-work-{os.getenv('APP_ENVIRONMENT', 'local')}-{provider}.lock"
        before = lock.stat()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "findb_fetcher.full_market_cli",
                "--config",
                str(CONFIG),
                "--provider",
                provider,
                "--execution-profile",
                profile,
                "--state-path",
                str(checkpoint),
                "--initialize-state",
            ],
            env={
                "PATH": os.environ["PATH"],
                "APP_ENVIRONMENT": "local",
                "FETCHER_ACCOUNT_STATE_PATH": str(governor),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["provider"] == provider
        assert checkpoint.read_bytes().startswith(b"SQLite format 3")
        assert (lock.stat().st_dev, lock.stat().st_ino) == (before.st_dev, before.st_ino)
        assert lock.read_bytes() == b"existing-lock-evidence"
        assert identity.endswith(f":{before.st_dev}:{before.st_ino}")
        with pytest.raises(provider_work.ProviderWorkWaitingError):
            with provider_work.host_lock(provider, root=account):
                pytest.fail("bootstrap released the active owner's lock")
    assert governor.read_bytes() == b"retained account budgets and cooldown"
    assert active_checkpoint.read_bytes() == b"retained prepared bodies and receipts"


def test_inode_initialization_is_idempotent_and_not_ownership(tmp_path):
    provider_work.initialize_host_lock("finlab", root=tmp_path)
    lock = next(tmp_path.iterdir())
    original = lock.stat()
    lock.write_bytes(b"retained")
    provider_work.initialize_host_lock("finlab", root=tmp_path)
    assert (lock.stat().st_dev, lock.stat().st_ino) == (original.st_dev, original.st_ino)
    assert lock.read_bytes() == b"retained"
    assert provider_work.inherited_fds() == ()
    with provider_work.host_lock("finlab", root=tmp_path):
        pass


def test_inode_initialization_rejects_symlink_without_replacing_it(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    target = tmp_path / "original"
    target.write_bytes(b"retained")
    lock = tmp_path / "provider-work-local-finlab.lock"
    lock.symlink_to(target)
    with pytest.raises(OSError):
        provider_work.initialize_host_lock("finlab", root=tmp_path)
    assert lock.is_symlink() and target.read_bytes() == b"retained"
