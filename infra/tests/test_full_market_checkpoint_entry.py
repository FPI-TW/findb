"""Transported read-only checkpoint safety for both execution profiles."""

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml
from script_env import script_env

REPO_ROOT = Path(__file__).resolve().parents[2]

GUARD = REPO_ROOT / "infra/deploy/check_full_market_checkpoint.sh"


def _workflow_steps():
    path = REPO_ROOT / ".github/workflows/fetcher-deploy.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)  # no YAML 1.1 booleans
    return workflow["jobs"]["deploy"]["steps"]


def _transport(tmp_path):
    # Execute the real SSM gate construction, not a separately rewritten policy.
    run = next(
        step["run"] for step in _workflow_steps() if "checkpoint_entry_gate=" in step.get("run", "")
    )
    start = run.index("guard=.trusted-checkpoint-safety/")
    end = run.index("host_command=", start)
    guard = tmp_path / ".trusted-checkpoint-safety/infra/deploy/check_full_market_checkpoint.sh"
    guard.parent.mkdir(parents=True)
    guard.write_bytes(GUARD.read_bytes())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Ubuntu uses GNU utilities. These finite local substitutes exercise exactly
    # the base64 arguments and digest comparison without AWS, Docker, or secrets.
    for name, body in {
        "base64": "import base64,sys\nif sys.argv[1:] == ['--decode']: sys.stdout.buffer.write(base64.b64decode(sys.stdin.buffer.read(),validate=True))\nelse:\n assert sys.argv[1]=='-w0'\n sys.stdout.buffer.write(base64.b64encode(open(sys.argv[2],'rb').read()))\n",
        "sha256sum": "import hashlib,sys\nprint(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest()+'  '+sys.argv[1])\n",
    }.items():
        file = binaries / name
        file.write_text("#!/usr/bin/env python3\n" + body)
        file.chmod(0o700)
    return run[start:end], str(binaries)


def test_workflow_gate_provenance_and_remote_order(tmp_path):
    steps = _workflow_steps()
    checkout = next(
        step for step in steps if step.get("name") == "Checkout trusted checkpoint safety gate"
    )
    assert checkout["with"]["repository"] == "${{ github.repository }}"
    assert checkout["with"]["ref"] == "${{ github.workflow_sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
    verify = next(
        step["run"]
        for step in steps
        if step.get("name") == "Verify trusted checkpoint safety provenance"
    )
    assert "inputs.revision" not in verify
    # Freeze the workflow object's bytes separately from its mutable checkout.
    # A tampered checkout must not become the fake git-show source of truth.
    names = ("check_full_market_checkpoint.sh", "check_fetcher_host.py", "release_manifest.py")
    pinned = tmp_path / "pinned-git-objects"
    for name in names:
        artifact = pinned / "infra/deploy" / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes((REPO_ROOT / "infra/deploy" / name).read_bytes())
    git_stub = r"""git() {
  [ "$1" = -C ] && [ "$2" = .trusted-checkpoint-safety ] || return 91
  if [ "$3" = rev-parse ] && [ "$4" = HEAD ]; then
    printf '%s\n' "$checkout_sha"
  elif [ "$3" = show ]; then
    case "$4" in
      "$WORKFLOW_SHA":infra/deploy/check_fetcher_host.py|"$WORKFLOW_SHA":infra/deploy/release_manifest.py)
        cat "$pinned_objects/${4#*:}" ;;
      *) return 92 ;;
    esac
  else return 93; fi
}
"""
    for wrong_sha, missing, tampered in (
        (False, False, None),
        (True, False, None),
        (False, True, None),
        (False, False, "check_fetcher_host.py"),
        (False, False, "release_manifest.py"),
    ):
        trusted = tmp_path / ".trusted-checkpoint-safety/infra/deploy"
        trusted.mkdir(parents=True, exist_ok=True)
        for name in names:
            (trusted / name).write_bytes((pinned / "infra/deploy" / name).read_bytes())
        if missing:
            (trusted / "check_full_market_checkpoint.sh").unlink()
        if tampered:
            (trusted / tampered).write_text("raise RuntimeError('untrusted checkout')")
        result = subprocess.run(
            ["/bin/bash", "-c", git_stub + verify],
            cwd=tmp_path,
            env={
                **script_env(),
                "WORKFLOW_SHA": "a" * 40,
                "checkout_sha": ("b" if wrong_sha else "a") * 40,
                "pinned_objects": str(pinned),
            },
            capture_output=True,
            timeout=10,
        )
        assert (result.returncode == 0) == (not wrong_sha and not missing and not tampered)
    run = next(step["run"] for step in steps if "checkpoint_entry_gate=" in step.get("run", ""))
    remote = next(line for line in run.splitlines() if line.startswith('host_command="set '))
    assert (
        remote.index("materialize-bundle")
        < remote.index("$checkpoint_entry_gate")
        < remote.index("/runtime-secrets/deploy_fetcher_aws.sh")
    )
    assert "guard=\\$(mktemp" in run
    assert 'base64 --decode > \\"\\$guard\\"' in run
    assert 'sha256sum \\"\\$guard\\"' in run


@pytest.mark.parametrize("target", ["staging", "production"])
@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("suffix", ["", "-previous", "-candidate", "-transaction-backup"])
@pytest.mark.parametrize(
    "state_kind", ["missing", "empty", "non-full", "valid", "corrupt", "unusable"]
)
def test_transported_checkpoint_guard(
    tmp_path,
    target,
    profile,
    suffix,
    state_kind,
    command_output=None,
    command_rc=0,
    inspect_rc=0,
    expected_reason=None,
):
    builder, binaries = _transport(tmp_path)
    checkpoint = tmp_path / "checkpoint/state.sqlite3"
    if state_kind != "missing":
        checkpoint.parent.mkdir()
        if state_kind == "empty":
            checkpoint.touch()
        else:
            with sqlite3.connect(checkpoint) as db:
                db.executescript(
                    "CREATE TABLE unrelated(value TEXT);"
                    if state_kind == "non-full"
                    else (
                        REPO_ROOT / "fetcher/tests/fixtures/full_market_legacy_core.sql"
                    ).read_text()
                )
    if state_kind == "unusable":
        with sqlite3.connect(checkpoint) as db:
            for table in ("full_work", "full_plan", "full_quota", "full_cursor"):
                db.execute(f"DROP TABLE {table}")
                db.execute(f"CREATE TABLE {table}(unusable TEXT)")
    if state_kind == "corrupt":
        with sqlite3.connect(checkpoint) as db:
            page = db.execute(
                "SELECT rootpage FROM sqlite_master WHERE name='full_work'"
            ).fetchone()[0]
            size = db.execute("PRAGMA page_size").fetchone()[0]
        with checkpoint.open("r+b") as raw:
            raw.seek((page - 1) * size)
            raw.write(b"\0")
    before = checkpoint.read_bytes() if checkpoint.exists() else None
    shell = r"""set -euo pipefail
export APP_ENVIRONMENT
sudo() {
  if [ "$1" = test ]; then
    local path="$3"
    if [ "$path" = "$expected_state_path" ]; then path="$checkpoint";
    elif [ "$path" = "${expected_state_path%/*}" ]; then path="${checkpoint%/*}"; else return 96; fi
    test "$2" "$path"
  elif [ "$1" = python3 ]; then python3 - "$checkpoint"; else return 95; fi
}
docker() {
  case "$1" in
    container) if [ "$2" = ls ]; then printf "%s\n" "$installed_name"; else [ "${!#}" = "$installed_name" ] && return "$inspect_rc"; fi ;;
    inspect) printf '%s\n' "$command_output"; return "$command_rc" ;;
    *) return 94 ;;
  esac
}
export -f sudo docker
"""
    shell += builder + '\neval "$checkpoint_entry_gate"\ntouch gate-passed\n'
    state_dir = f"/var/lib/findb-{profile.replace('_', '-')}/{target}"
    if profile == "full_market" and target == "production":
        state_dir = "/var/lib/findb-full-market"
    result = subprocess.run(
        ["/bin/bash", "-c", shell],
        cwd=tmp_path,
        env={
            **script_env(),
            "PATH": binaries + ":" + os.environ["PATH"],
            "APP_ENVIRONMENT": target,
            "installed_name": f"findb-{profile.replace('_', '-')}-finlab{suffix}",
            "command_output": command_output
            or f'["findb-fetch-runtime","--execution-profile","{profile}"]',
            "command_rc": str(command_rc),
            "inspect_rc": str(inspect_rc),
            "checkpoint": str(checkpoint),
            "expected_state_path": state_dir + "/state.sqlite3",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    if state_kind != "valid" or expected_reason:
        assert result.returncode != 0
        reason = (
            expected_reason
            or f"full_market_checkpoint_{'missing' if state_kind == 'missing' else 'invalid'}"
        )
        assert f"reason={reason}" in result.stderr
        assert "private-unverified-command" not in result.stderr
        assert not (tmp_path / "gate-passed").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "gate-passed").exists()
    assert (checkpoint.read_bytes() if checkpoint.exists() else None) == before


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "-previous",
        "-candidate",
        "-transaction-backup",
        "-preflight",
        "-preflight-state",
        "-preflight-control",
    ],
)
def test_transported_guard_rejects_unknown_command(tmp_path, profile, suffix):
    test_transported_checkpoint_guard(
        tmp_path,
        "staging",
        profile,
        suffix,
        "valid",
        command_output='["private-unverified-command"]',
        expected_reason="command_unknown",
    )


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("query", ["cmd", "listed-inspect"])
@pytest.mark.parametrize("rc", [1, 81])
def test_transported_guard_rejects_query_failure(tmp_path, profile, query, rc):
    test_transported_checkpoint_guard(
        tmp_path,
        "production",
        profile,
        "",
        "valid",
        command_rc=rc if query == "cmd" else 0,
        inspect_rc=rc if query == "listed-inspect" else 0,
        expected_reason="query_unknown",
    )


@pytest.mark.parametrize("tamper", ["digest", "payload", "missing"])
def test_gate_transport_integrity_fails_before_deployment_entry(tmp_path, tamper):
    builder, binaries = _transport(tmp_path)
    if tamper == "missing":
        (
            tmp_path / ".trusted-checkpoint-safety/infra/deploy/check_full_market_checkpoint.sh"
        ).unlink()
    mutate = {"digest": "q_guard_sha=bad", "payload": "q_guard_b64=invalid", "missing": ":"}[tamper]
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            "set -euo pipefail\n"
            + builder
            + f'\n{mutate}\neval "$checkpoint_entry_gate"\ntouch deployment-entry',
        ],
        cwd=tmp_path,
        env={**script_env(), "PATH": binaries + ":" + os.environ["PATH"]},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not (tmp_path / "deployment-entry").exists()


@pytest.mark.parametrize("mode", ["check", "require-stopped"])
def test_actual_docker_probes_accept_bash32_empty_cache_mount(tmp_path, mode):
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    marker = (
        'docker run --rm \\\n  --name "'
        + ("$preflight_name" if mode == "check" else "${preflight_name}-control")
        + '"'
    )
    start = helper.index(marker)
    command = helper[
        start : helper.index(f'"$image" "$@" --{mode}', start) + len(f'"$image" "$@" --{mode}')
    ]
    shell = (
        """set -euo pipefail
preflight_name=fixture; state_dir=/state; image=fixture-image
cache_mount=(); readiness_mount=(); runtime_env_args=(--env APP_ENVIRONMENT)
docker() { printf '%s\\n' "$@"; }
set -- findb-fetch-runtime --execution-profile pilot --state-path /state/state.sqlite3
"""
        + command
    )
    result = subprocess.run(
        ["/bin/bash", "-c", shell], capture_output=True, text=True, timeout=10, env=script_env()
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "--" + mode
    assert "" not in result.stdout.splitlines()
    assert "type=bind,src=/state,dst=/state" in result.stdout
