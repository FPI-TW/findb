"""Offline SDK packages for exec'd provider workers.

Workers start from a clean interpreter, so monkeypatches never cross into
them. Tests instead write fake SDK packages and expose them to worker launches
only, through ``PYTHONPATH`` added to the explicit provider environment.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
CA_BUNDLE = TESTS.parent / "certs" / "ap-southeast-1-bundle.pem"


class Frame:
    """Minimal pickleable DataFrame stand-in shared by parent and worker."""

    def __init__(self, day, value=42):
        self.index = (day,)
        self.columns = ("2330",)
        self.loc = {(day, "2330"): value}
        self.attrs = {"secret": "must-not-be-saved"}


def install_fake_sdk(monkeypatch, root: Path, files: dict[str, str]) -> list[subprocess.Popen]:
    """Write ``files`` below ``root`` and return the worker processes launched afterwards."""
    from findb_fetcher import provider_process

    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    environment = provider_process.provider_environment
    monkeypatch.setattr(
        provider_process,
        "provider_environment",
        lambda: {**environment(), "PYTHONPATH": os.pathsep.join((str(root), str(TESTS)))},
    )
    launched: list[subprocess.Popen] = []
    popen = provider_process.subprocess.Popen

    def launch(*args, **kwargs):
        launched.append(popen(*args, **kwargs))
        return launched[-1]

    monkeypatch.setattr(provider_process.subprocess, "Popen", launch)
    return launched


def _body(code: str) -> str:
    return textwrap.indent(textwrap.dedent(code).strip() or "pass", "    ")


def fake_finlab(get: str, *, login: str = "", check_version: str = "") -> dict[str, str]:
    """Pinned 1.5.7 layout: local login, ``data.get`` calling ``utils.check_version``."""
    prelude = "import os\nimport sys\nimport time\n\n"
    return {
        "finlab/__init__.py": prelude
        + "def login(token):\n"
        + _body(login + '\nos.environ["FINLAB_API_TOKEN"] = token[:64]')
        + "\n",
        "finlab/utils.py": prelude + "def check_version():\n" + _body(check_version) + "\n",
        "finlab/data.py": prelude
        + "import finlab.utils\n\n\ndef get(dataset):\n"
        + _body("finlab.utils.check_version()\n" + textwrap.dedent(get))
        + "\n",
    }


def child_deadline(deadline: float, now: float) -> float:
    """Map a fake-clock deadline onto the worker's real monotonic clock.

    Production compares one system-wide monotonic clock on both sides. Tests
    drive the parent with a fake clock; keep its verdict while giving a valid
    grant enough real time to survive IPC scheduling under load.
    """
    return time.monotonic() + (30 if deadline > now else -1)
