"""Read-only deployment safety before current and immutable legacy runtime code."""

import hashlib
import os
import sqlite3
import subprocess

import pytest

from tests.test_deployment_checks import REPO_ROOT, _load_workflow

GUARD = REPO_ROOT / "infra/deploy/check_full_market_checkpoint.sh"
FIXTURES = REPO_ROOT / "backend/tests/fixtures/legacy_full_checkpoint_replay"


def _workflow_steps():
    return _load_workflow(REPO_ROOT / ".github/workflows/fetcher-deploy.yml")["jobs"]["deploy"][
        "steps"
    ]


def _transport(tmp_path):
    # Execute the real SSM gate construction, not a separately rewritten policy.
    run = next(
        step["run"] for step in _workflow_steps() if "checkpoint_entry_gate=" in step.get("run", "")
    )
    start = run.index("guard=.trusted-checkpoint-safety/")
    end = run.index("# The downloaded immutable bundle", start)
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
    for wrong_sha, missing in [(False, False), (True, False), (False, True)]:
        guard = tmp_path / ".trusted-checkpoint-safety/infra/deploy/check_full_market_checkpoint.sh"
        guard.parent.mkdir(parents=True, exist_ok=True)
        guard.write_bytes(GUARD.read_bytes())
        if missing:
            guard.unlink()
        result = subprocess.run(
            ["/bin/bash", "-c", "git() { printf '%s\\n' \"$checkout_sha\"; }\n" + verify],
            cwd=tmp_path,
            env={
                **os.environ,
                "WORKFLOW_SHA": "a" * 40,
                "checkout_sha": ("b" if wrong_sha else "a") * 40,
            },
            capture_output=True,
            timeout=10,
        )
        assert (result.returncode == 0) == (not wrong_sha and not missing)
    run = next(step["run"] for step in steps if "checkpoint_entry_gate=" in step.get("run", ""))
    remote = next(line for line in run.splitlines() if line.startswith('host_command="set '))
    assert (
        remote.index("materialize-bundle")
        < remote.index("$checkpoint_entry_gate")
        < remote.index("$legacy_environment_bridge")
        < remote.index("/runtime-secrets/deploy_fetcher_aws.sh")
    )
    assert "guard=\\$(mktemp" in run
    assert 'base64 --decode > \\"\\$guard\\"' in run
    assert 'sha256sum \\"\\$guard\\"' in run


@pytest.mark.parametrize("profile", ["bounded", "full-market"])
@pytest.mark.parametrize(
    "name",
    [
        "findb-full-market-finlab",
        "findb-full-market-finlab-previous",
        "findb-fetcher-finlab-scheduler",
        "findb-fetcher-finlab-scheduler-previous",
    ],
)
@pytest.mark.parametrize(
    "state_kind", ["missing", "empty", "non-full", "valid", "corrupt", "unusable"]
)
def test_transported_gate_before_actual_immutable_coordinator(
    tmp_path,
    profile,
    name,
    state_kind,
    command_output=None,
    command_rc=0,
    inspect_rc=0,
    expected_reason=None,
):
    target = "production"  # The frozen pre-marker Full release supported production.
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
    # These are exact pre-marker accepted script/CLI excerpts, frozen independently
    # of HEAD. The trusted workflow gate precedes this OLD coordinator itself.
    coordinator = (FIXTURES / "coordinator.sh.txt").read_text()
    helper = (FIXTURES / "helper.sh.txt").read_text()
    fixture_digest = {
        file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in FIXTURES.iterdir()
    }
    events = tmp_path / "events"
    legacy_cli = tmp_path / "legacy_cli.py"
    legacy_cli.write_bytes((FIXTURES / "cli.py.txt").read_bytes())
    accepted_root = tmp_path / "immutable-bundle"
    accepted_coordinator = accepted_root / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh"
    accepted_coordinator.parent.mkdir(parents=True)
    accepted_coordinator.write_text(coordinator)
    workflow_run = next(
        step["run"] for step in _workflow_steps() if "checkpoint_entry_gate=" in step.get("run", "")
    )
    bridge = workflow_run[
        workflow_run.index("legacy_environment_bridge=") : workflow_run.index('host_command="set ')
    ]
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
    run)
      [ "${unsafe_witness:-false}" = true ] || return 94
      printf '%s\n' legacy-initializer >> "$events"
      while [ "$1" != findb-fetch-full-market ]; do shift; done
      shift
      local args=() arg
      while [ "$#" -gt 0 ]; do
        arg="$1"; shift
        case "$arg" in
          --config) shift; args+=(--config "$repo/fetcher/configs/full_market.production.v1.json") ;;
          --state-path) shift; args+=(--state-path "$checkpoint") ;;
          *) args+=("$arg") ;;
        esac
      done
      "$fetcher_python" "$legacy_cli" "${args[@]}"
      ;;
    *) printf '%s\n' forbidden-runtime-operation >> "$events"; return 94 ;;
  esac
}
export -f sudo docker
"""
    shell += builder + '\neval "$checkpoint_entry_gate"\n'
    shell += "MODE=replay\n" + bridge + '\neval "$legacy_environment_bridge"\n'
    shell += r"""
# The narrow verified replay bridge occurs only AFTER the safety gate.
[ "$DEPLOYMENT_TARGET" = "$APP_ENVIRONMENT" ]
printf '%s\n' immutable-old-coordinator >> "$events"
TWELVE_IMAGE_REF=accepted-twelve; FINLAB_IMAGE_REF=accepted-finlab; SHIOAJI_IMAGE_REF=accepted-shioaji
ECR_REGISTRY=fixture; provider_helper=legacy-accepted-helper
register_provider() { :; }
run_runtime() {
  while [ "$1" != -- ]; do shift; done
  shift 2
  provider="$1"; image="$2"; state_dir="$3"; state_path="$4"; stable="$5"; previous="$7"; preflight_name="$8"
  shift 9
  # Retain the old production layout; sudo maps only that exact mount below.
  [ "$provider" = finlab ] || return 0
  printf '%s\n' legacy-secret-wrapper >> "$events"
  cache_mount=(--mount fixture-cache); readiness_mount=()
"""
    shell += helper + "\n}\n" + coordinator
    result = subprocess.run(
        ["/bin/bash", "-c", shell],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": binaries + ":" + os.environ["PATH"],
            "APP_ENVIRONMENT": target,
            "FETCHER_RUNTIME_PROFILE": profile,
            "installed_name": name,
            "command_output": '["findb-fetch-full-market"]'
            if command_output is None
            else command_output,
            "command_rc": str(command_rc),
            "inspect_rc": str(inspect_rc),
            "checkpoint": str(checkpoint),
            "events": str(events),
            "root": str(accepted_root),
            "repo": str(REPO_ROOT),
            "fetcher_python": str(REPO_ROOT / "fetcher/.venv/bin/python"),
            "legacy_cli": str(legacy_cli),
            "expected_state_path": "/var/lib/findb-full-market"
            + ("/staging" if target == "staging" else "")
            + "/state.sqlite3",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    if state_kind != "valid" or expected_reason is not None:
        assert result.returncode != 0
        reason = (
            expected_reason
            or f"full_market_checkpoint_{'missing' if state_kind == 'missing' else 'invalid'}"
        )
        assert f"reason={reason}" in result.stderr
        assert "private-unverified-command" not in result.stderr
        assert not events.exists()  # No secret, rename/start, old initializer, Source or provider.
    else:
        assert result.returncode == 0, result.stderr
        assert events.read_text().splitlines()[0] == "immutable-old-coordinator"
    if (
        state_kind == "missing"
        and expected_reason is None
        and profile == "full-market"
        and name == "findb-fetcher-finlab-scheduler"
    ):
        # Direct witness: these same OLD accepted scripts/CLI really create lost
        # state without the independently transported current workflow gate.
        env = {
            **os.environ,
            "PATH": binaries + ":" + os.environ["PATH"],
            "APP_ENVIRONMENT": target,
            "FETCHER_RUNTIME_PROFILE": profile,
            "installed_name": name,
            "checkpoint": str(checkpoint),
            "events": str(events),
            "root": str(accepted_root),
            "repo": str(REPO_ROOT),
            "fetcher_python": str(REPO_ROOT / "fetcher/.venv/bin/python"),
            "legacy_cli": str(legacy_cli),
            "expected_state_path": "/var/lib/findb-full-market/state.sqlite3",
            "unsafe_witness": "true",
            "command_output": '["findb-fetch-full-market"]',
            "command_rc": "0",
            "inspect_rc": "0",
        }
        unsafe = subprocess.run(
            ["/bin/bash", "-c", shell.replace('eval "$checkpoint_entry_gate"', ":")],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert checkpoint.exists(), unsafe.stderr
        assert "legacy-initializer" in events.read_text()
        checkpoint.unlink()
        checkpoint.parent.rmdir()
    assert (checkpoint.read_bytes() if checkpoint.exists() else None) == before
    assert fixture_digest == {
        file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in FIXTURES.iterdir()
    }


@pytest.mark.parametrize(
    "base",
    [
        "findb-full-market-finlab",
        "findb-fetcher-finlab-scheduler",
        "findb-full-market-finlab-historical",
        "findb-fetcher-finlab-scheduler-historical",
    ],
)
@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "-previous",
        "-candidate",
        "-transaction-backup",
        "-legacy-previous",
        "-preflight",
        "-preflight-state",
        "-preflight-control",
    ],
)
def test_transported_guard_rejects_unknown_cmd_for_every_managed_name_before_legacy(
    tmp_path, base, suffix
):
    test_transported_gate_before_actual_immutable_coordinator(
        tmp_path,
        "full-market",
        base + suffix,
        "valid",
        command_output='["private-unverified-command"]',
        expected_reason="command_unknown",
    )


@pytest.mark.parametrize(
    "name",
    [
        "findb-fetcher-finlab-scheduler-historical",
        "findb-full-market-finlab-historical-candidate",
        "findb-fetcher-finlab-scheduler-preflight-control",
        "findb-full-market-finlab-transaction-backup",
    ],
)
@pytest.mark.parametrize("query", ["cmd", "listed-inspect"])
@pytest.mark.parametrize("rc", [1, 81])
def test_transported_guard_rejects_managed_name_query_failure_before_legacy(
    tmp_path, name, query, rc
):
    test_transported_gate_before_actual_immutable_coordinator(
        tmp_path,
        "full-market",
        name,
        "valid",
        command_rc=rc if query == "cmd" else 0,
        inspect_rc=rc if query == "listed-inspect" else 0,
        expected_reason="query_unknown",
    )


@pytest.mark.parametrize(
    "name,command",
    [
        (
            "findb-fetcher-finlab-scheduler-historical",
            '["findb-fetch-historical-backfill","--provider","finlab","--run-forever"]',
        ),
        (
            "findb-fetcher-finlab-scheduler-preflight-control",
            '["findb-fetch-finlab-scheduler","--check"]',
        ),
        ("findb-full-market-finlab-historical-candidate", '["findb-fetch-full-market","--check"]'),
    ],
)
def test_transported_guard_preserves_legal_derived_roles_and_legacy_entry(tmp_path, name, command):
    test_transported_gate_before_actual_immutable_coordinator(
        tmp_path, "full-market", name, "valid", command_output=command
    )


@pytest.mark.parametrize("tamper", ["digest", "payload", "missing"])
def test_gate_transport_integrity_fails_before_legacy_entry(tmp_path, tamper):
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
            + f'\n{mutate}\neval "$checkpoint_entry_gate"\ntouch legacy-entry',
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": binaries + ":" + os.environ["PATH"]},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert not (tmp_path / "legacy-entry").exists()


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
set -- findb-fetch-full-market --state-path /state/state.sqlite3
"""
        + command
    )
    result = subprocess.run(["/bin/bash", "-c", shell], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "--" + mode
    assert "" not in result.stdout.splitlines()
    assert "type=bind,src=/state,dst=/state" in result.stdout
