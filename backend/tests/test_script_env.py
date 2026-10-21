"""Scripts under test must not inherit the developer or CI environment."""

import re
from pathlib import Path

from tests.script_env import script_env

REPO_ROOT = Path(__file__).resolve().parents[2]
FULL_ENVIRONMENT = re.compile(
    r"\*\*os\.environ\b|dict\(\s*os\.environ\b|os\.environ\s*\||os\.environ\.copy\(\)"
)
# These run the repository's own Python/pytest with the test configuration, or
# embed the line in a fake `docker exec` script that models the container.
ALLOWED_FILES = {
    "backend/tests/migration_database.py",
    "backend/tests/test_orm_database_isolation.py",
}
ALLOWED_LINES = {
    ("backend/tests/test_staging_feed_evidence.py", '"environment = dict(os.environ)\\n"')
}


def test_script_env_passes_only_process_plumbing(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "leaked")
    monkeypatch.setenv("GITHUB_OUTPUT", "/leaked")
    environment = script_env()
    assert "AWS_PROFILE" not in environment and "GITHUB_OUTPUT" not in environment
    assert set(environment) <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"}


def test_shell_script_tests_do_not_inherit_the_full_environment():
    offenders = []
    for folder in ("backend/tests", "infra/tests"):
        for path in sorted((REPO_ROOT / folder).glob("*.py")):
            name = path.relative_to(REPO_ROOT).as_posix()
            if name in ALLOWED_FILES or path == Path(__file__).resolve():
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if FULL_ENVIRONMENT.search(line) and (name, line.strip()) not in ALLOWED_LINES:
                    offenders.append(f"{name}:{number}: {line.strip()}")
    assert not offenders, "use script_env() instead:\n" + "\n".join(offenders)
