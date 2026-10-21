"""Minimal environment for shell and deployment scripts under test.

Inheriting ``os.environ`` lets developer or CI settings (``AWS_*``,
``APP_ENVIRONMENT``, ``FETCHER_*``, ``GITHUB_*``, ``ACTIONS_*``) change what a
script does. Only process plumbing passes through; tests set everything else.
Keep in sync with ``backend/tests/script_env.py``.
"""

import os

PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


def script_env() -> dict[str, str]:
    return {key: os.environ[key] for key in PASSTHROUGH if key in os.environ}
