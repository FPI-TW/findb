"""Scripts under test must not inherit the developer or CI environment."""

import ast
import re
from pathlib import Path

from tests.script_env import script_env

REPO_ROOT = Path(__file__).resolve().parents[2]
FULL_ENVIRONMENT = re.compile(
    r"\*\*os\.environ\b|dict\(\s*os\.environ\b|os\.environ\s*\||os\.environ\.copy\(\)"
    r"|env\s*=\s*os\.environ\s*[,)]"
)
SUBPROCESS_CALLS = {"run", "check_output", "check_call", "call", "Popen"}
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


def _test_sources():
    for folder in ("backend/tests", "infra/tests"):
        for path in sorted((REPO_ROOT / folder).glob("*.py")):
            name = path.relative_to(REPO_ROOT).as_posix()
            if name not in ALLOWED_FILES and path != Path(__file__).resolve():
                yield name, path.read_text()


def test_shell_script_tests_do_not_inherit_the_full_environment():
    offenders = []
    for name, source in _test_sources():
        for number, line in enumerate(source.splitlines(), 1):
            if FULL_ENVIRONMENT.search(line) and (name, line.strip()) not in ALLOWED_LINES:
                offenders.append(f"{name}:{number}: {line.strip()}")
    assert not offenders, "use script_env() instead:\n" + "\n".join(offenders)


def test_every_subprocess_call_states_its_environment():
    # Omitting env= silently inherits os.environ, exactly like {**os.environ}.
    offenders = [
        f"{name}:{node.lineno}"
        for name, source in _test_sources()
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr in SUBPROCESS_CALLS
        and not any(keyword.arg == "env" for keyword in node.keywords)
    ]
    assert not offenders, "pass env=script_env() (plus explicit variables):\n" + "\n".join(
        offenders
    )
