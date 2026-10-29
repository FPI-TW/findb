"""Retired entry points load no legacy runtime and cannot acquire or alter state."""

import json
import os
import shutil
import subprocess
import sys

import pytest

COMMANDS = {
    "scheduler_cli": "findb-fetch-scheduler",
    "finlab_scheduler_cli": "findb-fetch-finlab-scheduler",
    "shioaji_scheduler_cli": "findb-fetch-shioaji-scheduler",
    "shioaji_staging_cli": "findb-fetch-shioaji-staging",
    "shioaji_smoke_cli": "findb-fetch-shioaji-smoke",
    "finlab_smoke_cli": "findb-fetch-finlab-smoke",
    "twelve_data_cli": "findb-fetch-twelve-data",
    "taifex_pilot": "findb-fetch-taifex-pilot",
}


def assert_retired(result, command):
    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    body = json.loads(result.stderr)
    assert body["status"] == "retired" and body["command"] == command
    assert body["runtime"] == "findb-fetch-runtime"
    assert body["historical"] == "findb-fetch-historical-backfill"
    assert len(result.stderr.encode()) < 1024


@pytest.mark.parametrize("module,command", COMMANDS.items())
def test_tombstone_adds_no_legacy_implementation_or_imports(tmp_path, module, command):
    script = """
import importlib.abc
import sys
import findb_fetcher

class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('findb_fetcher.') and fullname not in {
            'findb_fetcher.' + sys.argv[1], 'findb_fetcher.legacy_cli'
        }:
            raise AssertionError('retired command imported runtime: ' + fullname)
        if fullname.split('.')[0] in {'argparse','boto3','httpx','multiprocessing','sqlite3','finlab','shioaji'}:
            raise AssertionError('retired command imported dependency: ' + fullname)

sys.meta_path.insert(0, NoRuntime())
module = __import__('findb_fetcher.' + sys.argv[1], fromlist=['main'])
assert [name for name, value in vars(module).items()
        if callable(value) and getattr(value, '__module__', None) == module.__name__] == ['main']
raise SystemExit(module.main(['--deliver', '--run-forever']))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, module],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert_retired(result, command)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("module,command", COMMANDS.items())
@pytest.mark.parametrize("entry", ["console", "module"])
def test_installed_tombstone_and_python_module_preserve_existing_state(
    tmp_path, module, command, entry
):
    checkpoint = tmp_path / "checkpoint.sqlite3"
    original = b"retained checkpoint/prepared/receipts/budget bytes"
    checkpoint.write_bytes(original)
    executable = shutil.which(command)
    assert executable
    argv = [executable] if entry == "console" else [sys.executable, "-m", "findb_fetcher." + module]
    result = subprocess.run(
        [*argv, "--help"],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "FETCHER_STATE_PATH": str(checkpoint),
            "PROVIDER_WORK_REQUIRED": "true",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert_retired(result, command)
    assert checkpoint.read_bytes() == original
    assert list(tmp_path.iterdir()) == [checkpoint]
