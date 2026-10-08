"""Actual Docker arguments and legacy-name transaction recovery, entirely offline."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_deployment_checks import _FAKE_DOCKER, REPO_ROOT


def _inventory_policy(source):
    start = source.index("# A failed inspect is not proof of absence:")
    end = source.index('\n: "${', start)
    return "\n" + source[start:end] + "\n"


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji"])
def test_production_container_arguments_bind_full_pilot_historical_inspection(
    tmp_path, monkeypatch, provider
):
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    environment_args = helper[
        helper.index("runtime_env_args=(\n") : helper.index('\nsudo mkdir -p "$state_dir"')
    ]
    common = helper[
        helper.index("common_args=(\n") : helper.index('\nif [ "$accepted_release" = false ]; then')
    ]
    normal = helper[
        helper.index('docker run -d --name "$candidate"') : helper.index(
            "\ncandidate_image=", helper.index('docker run -d --name "$candidate"')
        )
    ]
    historical = helper[
        helper.index('docker run -d --name "$historical_name"') : helper.index(
            '\nif [ "$(docker inspect', helper.index('docker run -d --name "$historical_name"')
        )
    ]
    names = {
        "twelve_data": "findb-fetcher-scheduler",
        "finlab": "findb-fetcher-finlab-scheduler",
        "shioaji": "findb-fetcher-shioaji-scheduler",
    }
    governor = "/var/lib/findb-account/production/governor.sqlite3"
    image = "image@sha256:" + "b" * 64
    manifest = {
        "deployment_target": "production",
        "runtime_profile": "full-market",
        "unit": "fetcher",
        "commit_sha": "a" * 40,
        "images": {provider: image},
    }
    accepted = {**manifest, "state": "accepted", "bundle_sha256": "c" * 64}
    config = {"deployment_target": "production", "desired_state": "stopped"}
    allocation = {
        "provider": provider,
        "environment": "production",
        "requests_per_second": 3.0,
        "source_requests_per_minute": 120,
        "account": {
            "governor_identity": governor,
            "consumers": ["full_market", "pilot", "historical", "maintenance"],
        },
    }
    for name, value in [
        ("manifest", manifest),
        ("accepted", accepted),
        ("config", config),
        ("allocation", allocation),
    ]:
        (tmp_path / name).write_text(json.dumps(value))
    actual = {}
    for consumer in ["full_market", "pilot", "historical"]:
        full_name = "findb-full-market-" + provider.replace("_", "-")
        name = (
            full_name
            if consumer == "full_market"
            else names[provider] + ("-historical" if consumer == "historical" else "")
        )
        args_env = {
            **os.environ,
            "APP_ENVIRONMENT": "production",
            "FULL_MARKET_ENABLED": "true",
            "FETCHER_CONSUMER_PROFILE": "full_market" if consumer == "full_market" else "pilot",
            "FETCHER_ACCOUNT_STATE_PATH": governor,
            "SOURCE_CLIENT_KEY": "source-fixture",
        }
        provider_env = {
            "twelve_data": "TWELVE_DATA_API_KEY",
            "finlab": "FINLAB_API_TOKEN",
            "shioaji": "SHIOAJI_API_KEY SHIOAJI_SECRET_KEY SHIOAJI_SIMULATION",
        }[provider]
        shell = "\n".join(
            [
                "set -euo pipefail",
                f"provider={provider.replace('_', '-')}",
                f"candidate={name}; historical_name={name}",
                f"image={image}",
                "accepted_release=true",
                "state_dir=/checkpoint; cache_dir=-; historical_state_dir=/checkpoint/historical",
                "provider_env=(" + " ".join("--env " + key for key in provider_env.split()) + ")",
                f"account_mount=(--mount type=bind,src={Path(governor).parent},dst={Path(governor).parent})",
                "readiness_mount=(--mount type=bind,src=/etc/readiness,dst=/var/lib/findb-account/readiness,readonly)",
                "historical_provider=" + provider,
                "set -- findb-fetch-full-market --config /installed/config"
                if consumer == "full_market"
                else "set -- findb-fetch-scheduler",
                'docker() { printf "%s\\n" "$@"; }',
                environment_args,
                common,
                historical if consumer == "historical" else normal,
            ]
        )
        # Actual command redirects stdout, so capture through a shell file descriptor.
        shell = shell.replace(
            'docker() { printf "%s\\n" "$@"; }', 'docker() { printf "%s\\n" "$@" >&3; }'
        )
        completed = subprocess.run(
            ["/bin/bash", "-c", "exec 3>&1\n" + shell],
            env=args_env,
            capture_output=True,
            text=True,
            check=True,
        )
        arguments = completed.stdout.splitlines()
        env = {}
        mounts = []
        for index, arg in enumerate(arguments[:-1]):
            if arg == "--env":
                key, sep, value = arguments[index + 1].partition("=")
                if sep or key in args_env:
                    env[key] = value if sep else args_env[key]
            if arg == "--mount":
                fields = dict(
                    item.split("=", 1) for item in arguments[index + 1].split(",") if "=" in item
                )
                mounts.append(
                    {"Destination": fields["dst"], "RW": "readonly" not in arguments[index + 1]}
                )
        assert env["FETCHER_CONSUMER_PROFILE"] == consumer
        assert set(provider_env.split()) <= set(arguments)
        assert "FINLAB_API_TOKEN" not in arguments if provider != "finlab" else True
        actual[name] = {
            "Id": name + "-id",
            "Config": {
                "Image": image,
                "Labels": {"com.findb.fetcher.accepted": "true"},
                "Env": [key + "=" + value for key, value in env.items()],
                "Cmd": ["findb-fetch-full-market", "--config", "/installed/config"],
            },
            "Mounts": mounts,
            "State": {"Running": True},
        }
    spec = importlib.util.spec_from_file_location(
        "inspection_repair", REPO_ROOT / "infra/deploy/inspect_full_market_installation.py"
    )
    inspection = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inspection)
    monkeypatch.setattr(inspection, "inspect", lambda name: actual[name])
    allocation_digest = hashlib.sha256(
        json.dumps(allocation, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    monkeypatch.setattr(
        inspection.subprocess,
        "check_output",
        lambda command, **kwargs: (
            (
                inspection.digest(tmp_path / "config")
                if command[-1] == "/installed/config"
                else allocation_digest
            )
            + "\n"
        ),
    )
    result = inspection.build(
        tmp_path / "manifest",
        tmp_path / "accepted",
        tmp_path / "config",
        provider,
        governor,
        tmp_path / "allocation",
    )
    assert result["consumers"] == ["full_market", "pilot", "historical", "maintenance"]
    assert len(result["installed_consumers"]) == 3


@pytest.mark.parametrize("profile", ["full-market", "bounded"])
@pytest.mark.parametrize(
    "boundary",
    [
        "success",
        "stop-failure",
        "retirement-failure",
        "rename-failure",
        "after-rename",
        "signal-stop",
        "signal-rename",
        "after-replacement",
        "signal-replacement",
    ],
)
@pytest.mark.parametrize("initial_running", [True, False])
@pytest.mark.parametrize("stale_previous", [False, True])
def test_legacy_full_runtime_upgrade_or_flagoff_recovers_original_identity(
    tmp_path, profile, boundary, initial_running, stale_previous
):
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
    transaction = (
        _inventory_policy(helper)
        + helper.split("processed=()", 1)[1].split("retire_runtime() {", 1)[0]
    )
    move = re.search(r"move_legacy_full_runtime\(\) \{\n.*?\n\}", helper, re.S).group()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy = state_dir / "legacy"
    legacy.write_text("running\n" if initial_running else "stopped\n")
    original_inode = legacy.stat().st_ino
    previous = state_dir / "legacy-previous"
    previous_inode = None
    if stale_previous:
        previous.write_text("stopped\n")
        previous_inode = previous.stat().st_ino
    checkpoint = state_dir / "checkpoint.sqlite3"
    checkpoint.write_bytes(b"accepted prepared bodies")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = _FAKE_DOCKER
    if boundary == "stop-failure":
        docker = docker.replace(
            'name="$3"',
            'name="$3"\n    [ ! -e "$state_dir/stop-failed" ] || exit 1\n    touch "$state_dir/stop-failed"\n    exit 1',
            1,
        )
    if boundary == "rename-failure":
        docker = docker.replace('mv "$state_dir/$1" "$state_dir/$2"', "exit 1")
    (fake_bin / "docker").write_text(docker)
    (fake_bin / "docker").chmod(0o755)
    suffix = (
        "false"
        if boundary == "after-rename"
        else "trap - ERR INT TERM HUP\ncleanup_legacy_previous_backups"
    )
    if boundary in {"after-replacement", "signal-replacement"}:
        suffix = f"""register_provider full full-previous
docker rename full full-previous
printf 'running\n' > {state_dir}/full
register_provider legacy legacy-previous
printf 'running\n' > {state_dir}/legacy
{"kill -TERM $$" if boundary == "signal-replacement" else "false"}
"""
    shell = f"set -Eeuo pipefail\nFETCHER_DEPLOY_MODE=candidate\nFETCHER_RUNTIME_PROFILE={profile}\nprocessed=(){transaction}\n{move}\nmove_legacy_full_runtime legacy full\n{suffix}\n"
    env = {
        **os.environ,
        "PATH": str(fake_bin) + ":" + os.environ["PATH"],
        "FAKE_DOCKER_STATE": str(state_dir),
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_IMAGE": "accepted-image@sha256:fixture",
        "FAKE_STABLE_EXIT_CODE": "1" if boundary == "retirement-failure" else "0",
    }
    if boundary in {"signal-stop", "signal-rename"}:
        env.update(
            FAKE_SIGNAL_ON=boundary.split("-")[1], FAKE_SIGNAL_SENT=str(tmp_path / "signal-sent")
        )
    completed = subprocess.run(
        ["/bin/bash", "-c", shell], env=env, capture_output=True, text=True, timeout=10
    )
    if boundary == "success" or (
        boundary in ["stop-failure", "signal-stop"] and not initial_running
    ):
        assert completed.returncode == 0, completed.stderr
        assert (state_dir / "full").stat().st_ino == original_inode
        assert not legacy.exists()
        assert (state_dir / "full").read_text().strip() == "stopped"
    else:
        assert completed.returncode != 0
        assert legacy.stat().st_ino == original_inode
        assert legacy.read_text().strip() == ("running" if initial_running else "stopped")
        assert not (state_dir / "full").exists()
    if stale_previous:
        success = boundary == "success" or (
            boundary in ["stop-failure", "signal-stop"] and not initial_running
        )
        if success:
            assert not (state_dir / "full-legacy-previous").exists()
            assert not previous.exists()
        else:
            assert previous.stat().st_ino == previous_inode
            assert previous.read_text().strip() == "stopped"
            assert not (state_dir / "full-legacy-previous").exists()
    assert checkpoint.read_bytes() == b"accepted prepared bodies"


@pytest.mark.parametrize("target", ["local", "staging", "production"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("provider", ["twelve_data", "finlab"])
@pytest.mark.parametrize("consumer", ["full_market", "pilot", "historical", None])
def test_actual_full_state_bootstrap_forwards_only_canonical_environment(
    tmp_path, target, existing, provider, consumer
):
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    start = helper.index("state_bootstrap=false\n")
    bootstrap = helper[start:].split('\ndocker run --rm \\\n  --name "$preflight_name"', 1)[0]
    state = tmp_path / "state.sqlite3"
    log = tmp_path / "docker-args"
    if existing:
        state.write_bytes(b"existing checkpoint must not be rewritten")
    script = (
        """set -euo pipefail
stable=stable
preflight_name=preflight
image=reviewed-image
cache_mount=()
account_mount=()
readiness_mount=()
runtime_env_args=(--env SOURCE_SECRET=must-not-be-injected)
sudo() {
  if [ "$1" = stat ]; then printf '%s\\n' '10001:10001:600'; else "$@"; fi
}
docker() {
  if [ "$1" = container ]; then [ "$2" != ls ] || return 0; return 1; fi
  printf '%s\\n' "$@" > "$command_log"
  touch "$state_path"
}
"""
        + "\n"
    )
    config = REPO_ROOT / f"fetcher/configs/full_market.{target}.v1.json"
    if consumer == "full_market":
        command = ["findb-fetch-full-market", "--config", str(config), "--provider", provider]
    elif consumer == "historical":
        command = ["findb-fetch-historical-backfill", "--provider", provider, "--run-forever"]
    else:
        command = [
            "findb-fetch-scheduler"
            if provider == "twelve_data"
            else "findb-fetch-finlab-scheduler",
            "--schedule-file",
            str(REPO_ROOT / "fetcher/configs/daily_scheduler.staging.v3.json"),
            "--slot-id",
            "western_markets_window" if provider == "twelve_data" else "taiwan_market_window",
            "--dataset-key",
            "us_equity_eod" if provider == "twelve_data" else "tw_equity_eod",
        ]
    environment = {**os.environ}
    environment.pop("FETCHER_CONSUMER_PROFILE", None)
    if consumer is not None:
        environment["FETCHER_CONSUMER_PROFILE"] = consumer
    script += _inventory_policy(helper) + 'set -- "$@"\n' + bootstrap
    result = subprocess.run(
        ["/bin/bash", "-c", script, "bootstrap", *command],
        env={
            **environment,
            "provider": provider.replace("_", "-"),
            "APP_ENVIRONMENT": target,
            "FETCHER_RUNTIME_PROFILE": "full-market",
            "state_path": str(state),
            "state_dir": str(tmp_path),
            "command_log": str(log),
            "config_path": str(config),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    if existing or consumer != "full_market":
        assert not log.exists()
        if existing:
            assert state.read_bytes() == b"existing checkpoint must not be rewritten"
        else:
            assert not state.exists()
        if consumer in {"pilot", None}:
            module = "scheduler_cli" if provider == "twelve_data" else "finlab_scheduler_cli"
            parsed = subprocess.run(
                [
                    str(REPO_ROOT / "fetcher/.venv/bin/python"),
                    "-c",
                    f"from findb_fetcher.{module} import build_parser; import sys; build_parser().parse_args(sys.argv[1:])",
                    *command[1:],
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert parsed.returncode == 0, parsed.stderr
        return
    arguments = log.read_text().splitlines()
    environment = {}
    for index, argument in enumerate(arguments[:-1]):
        if argument == "--env":
            key, separator, value = arguments[index + 1].partition("=")
            assert separator
            environment[key] = value
    assert environment == {"APP_ENVIRONMENT": target}
    assert "SOURCE_SECRET=must-not-be-injected" not in arguments
    state.unlink()  # Execute the real CLI with exactly the extracted Docker environment.
    cli = arguments[arguments.index("findb-fetch-full-market") + 1 :]
    completed = subprocess.run(
        [str(REPO_ROOT / "fetcher/.venv/bin/python"), "-m", "findb_fetcher.full_market_cli", *cli],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert state.read_bytes().startswith(b"SQLite format 3")


@pytest.mark.parametrize(
    "consumer,command,profile,recorded,allowed",
    [
        ("full_market", "findb-fetch-scheduler", "full-market", None, False),
        ("full_market", "findb-fetch-finlab-scheduler", "full-market", None, False),
        ("pilot", "findb-fetch-full-market", "full-market", None, False),
        ("historical", "findb-fetch-full-market", "full-market", None, False),
        (None, "findb-fetch-full-market", "full-market", None, False),
        ("full_market", "findb-fetch-full-market", "bounded", None, True),
        ("full_market", "findb-fetch-full-market", "full-market", "bounded", True),
    ],
)
def test_bootstrap_rejects_mismatched_full_consumer_and_preserves_bounded_drain_state(
    tmp_path, consumer, command, profile, recorded, allowed
):
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    block = helper[helper.index("state_bootstrap=false\n") :].split(
        '\ndocker run --rm \\\n  --name "$preflight_name"', 1
    )[0]
    environment = {**os.environ}
    environment.pop("FETCHER_CONSUMER_PROFILE", None)
    environment.pop("FETCHER_RECORDED_RELEASE_PROFILE", None)
    environment.update(
        provider="twelve-data",
        FETCHER_RUNTIME_PROFILE=profile,
        state_path=str(tmp_path / "missing"),
    )
    if consumer:
        environment["FETCHER_CONSUMER_PROFILE"] = consumer
    if recorded:
        environment["FETCHER_RECORDED_RELEASE_PROFILE"] = recorded
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -euo pipefail\nsudo() { "$@"; }\ndocker() { exit 99; }\n' + block,
            "bootstrap",
            command,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (0 if allowed else 2), result.stderr
    if not allowed:
        assert "reason=consumer_command_mismatch" in result.stderr
    assert not (tmp_path / "missing").exists()


def test_coordinator_passes_consumer_identity_independently_of_full_release_profile():
    deploy = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
    full = deploy.index("export FETCHER_CONSUMER_PROFILE=full_market")
    pilot = deploy.index("export FETCHER_CONSUMER_PROFILE=pilot", full)
    assert full < deploy.index("findb-fetch-full-market --config", full) < pilot
    assert 'export FETCHER_RUNTIME_PROFILE="$recorded_profile"' in deploy[pilot - 100 : pilot]
    assert pilot < deploy.index("findb-fetch-scheduler --schedule-file", pilot)
    assert pilot < deploy.index("findb-fetch-finlab-scheduler --schedule-file", pilot)
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    historical = helper[helper.index('docker run -d --name "$historical_name"') :]
    assert "--env FETCHER_CONSUMER_PROFILE=historical" in historical
    assert "findb-fetch-historical-backfill" in historical
    assert "--initialize-state" not in historical


@pytest.mark.parametrize("target", ["staging", "production"])
@pytest.mark.parametrize("profile", ["bounded", "full-market"])
@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("legacy_name", [False, True])
@pytest.mark.parametrize(
    "state_kind", ["missing", "valid", "empty", "non-full", "malformed", "corrupt", "unusable"]
)
@pytest.mark.parametrize("older_helper", [False, True])
@pytest.mark.parametrize("previous_only", [False, True])
def test_actual_coordinator_retained_checkpoint_guard_precedes_helper_and_control(
    tmp_path, target, profile, replay, legacy_name, state_kind, older_helper, previous_only
):
    import sqlite3

    deploy = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    coordinator = _inventory_policy(deploy) + (
        deploy[
            deploy.index('recorded_profile="$FETCHER_RUNTIME_PROFILE"') : deploy.index(
                "processed=()"
            )
        ]
        + deploy[
            deploy.index("# Preserve historical workers") : deploy.index("# Pilot runtimes coexist")
        ]
    )
    retirement = re.search(r"require_retired_runtime\(\) \{\n.*?\n\}", deploy, re.S).group()
    move = re.search(r"move_legacy_full_runtime\(\) \{\n.*?\n\}", deploy, re.S).group()
    bootstrap = helper[helper.index("state_bootstrap=false\n") :].split(
        '\ndocker run --rm \\\n  --name "$preflight_name"', 1
    )[0]
    if older_helper:
        # A compatible immutable helper predates recorded-profile dispatch.
        # Its unsafe initializer is protected by the current caller precondition.
        bootstrap = 'if [ "$FETCHER_RUNTIME_PROFILE" = full-market ] && ! sudo test -e "$state_path"; then\n  docker run "$image" "$@" --initialize-state\nfi\n'
    control = helper[
        helper.index('docker run --rm \\\n  --name "${preflight_name}-control"') : helper.index(
            "\ncommon_args=("
        )
    ]
    checkpoint = tmp_path / "checkpoint" / "state.sqlite3"
    existing = state_kind == "valid"
    if state_kind != "missing":
        checkpoint.parent.mkdir()
    if state_kind in {"empty", "malformed"}:
        checkpoint.write_bytes(b"" if state_kind == "empty" else b"invalid SQLite")
    elif state_kind == "non-full":
        with sqlite3.connect(checkpoint) as db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
    if state_kind == "unusable":
        with sqlite3.connect(checkpoint) as db:
            for table in ("full_work", "full_plan", "full_quota", "full_cursor"):
                db.execute(f"CREATE TABLE {table}(unusable TEXT)")
    if state_kind in {"valid", "corrupt"}:
        with sqlite3.connect(checkpoint) as db:
            # Core tables are stable across older compatible Full checkpoints;
            # the read-only probe must not require newer capacity/repair tables.
            db.executescript(
                (REPO_ROOT / "fetcher/tests/fixtures/full_market_legacy_core.sql").read_text()
            )

    if state_kind == "corrupt":
        with sqlite3.connect(checkpoint) as db:
            page = db.execute(
                "SELECT rootpage FROM sqlite_master WHERE name='full_work'"
            ).fetchone()[0]
            size = db.execute("PRAGMA page_size").fetchone()[0]
        with checkpoint.open("r+b") as raw:
            raw.seek((page - 1) * size)
            raw.write(b"\0")
    original_checkpoint = checkpoint.read_bytes() if checkpoint.exists() else None
    containers = tmp_path / "containers"
    containers.mkdir()
    original_name = "findb-fetcher-scheduler" if legacy_name else "findb-full-market-twelve-data"
    if previous_only:
        original_name += "-previous"
    original = containers / original_name
    original.write_text("accepted original image/id")
    original_inode = original.stat().st_ino
    historical = []
    for name in [
        "findb-fetcher-scheduler",
        "findb-fetcher-finlab-scheduler",
        "findb-fetcher-shioaji-scheduler",
    ]:
        path = containers / (name + "-historical")
        path.write_text("accepted stopped historical")
        historical.append((path, path.stat().st_ino))
    events = tmp_path / "events"
    cli = tmp_path / "control.py"
    cli.write_text("""
import os, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from findb_fetcher import full_market_cli
class Control:
    def __init__(self, *_): pass
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def poll(self, **kwargs):
        with open(os.environ['events'], 'a') as output: output.write('Source-control\\n')
        provider = args[args.index('--provider')+1]
        return SimpleNamespace(provider=provider, dataset_keys=sorted(full_market_cli._SCOPES[provider]), desired_state='stopped')
args = sys.argv[1:]
args[args.index('--config')+1] = str(Path(os.environ['repo'])/f"fetcher/configs/full_market.{os.environ['APP_ENVIRONMENT']}.v1.json")
assert args[args.index('--state-path')+1] == os.environ['expected_state_path']
args[args.index('--state-path')+1] = os.environ['checkpoint']
with patch.object(full_market_cli.FetcherConfig, 'from_env', lambda: object()), patch.object(full_market_cli, 'SchedulerControlClient', Control), patch.object(full_market_cli, 'FullMarketState', side_effect=AssertionError('control cannot construct state')):
    raise SystemExit(full_market_cli.main(args))
""")
    shell = r"""set -euo pipefail
export APP_ENVIRONMENT
export FULL_MARKET_ENABLED=false
TWELVE_IMAGE_REF=recorded-bundle-twelve
FINLAB_IMAGE_REF=recorded-bundle-finlab
SHIOAJI_IMAGE_REF=recorded-bundle-shioaji
ECR_REGISTRY=fixture-registry
provider_helper=fixture-helper
legacy_moves=()
legacy_previous_backups=()
processed=()
named_full_originals=()
named_full_backups=()
rollback_failed=0
sudo() {
  if [ "$1" = test ]; then
    local op="$2" path="$3"
    printf '%s\n' "$path" >> "$probe_paths"
    if [ "$path" = "$expected_state_path" ]; then path="$checkpoint";
    elif [ "$path" = "${expected_state_path%/*}" ]; then path="${checkpoint%/*}";
    else return 95; fi
    test "$op" "$path"
  elif [ "$1" = python3 ]; then python3 - "$checkpoint"
  elif [ "$1" = stat ]; then printf '%s\n' '10001:10001:600'; else return 98; fi
}
docker() {
  local name="${!#}"
  case "$1" in
    container)
      if [ "$2" = ls ]; then
        for entry in "$containers"/*; do [ ! -f "$entry" ] || basename "$entry"; done
      else test -f "$containers/$name"; fi ;;
    inspect)
      case "$3" in
        *Config.Cmd*) printf '%s\n' '["findb-fetch-full-market"]' ;;
        *Config.Image*) printf '%s\n' installed-compatible-image ;;
        *Config.Labels*) printf '%s\n' true ;;
        *State.Running*|*State.OOMKilled*) printf '%s\n' false ;;
        *State.ExitCode*) printf '%s\n' 0 ;;
        *State.Error*) printf '\n' ;;
        *'{{.Id}}'*) python3 -c 'import os,sys; print("fixture-"+str(os.stat(sys.argv[1]).st_ino))' "$containers/$name" ;;
        *) return 97 ;;
      esac ;;
    rename) printf '%s\n' renamed >> "$events"; mv "$containers/$2" "$containers/$3" ;;
    start) printf '%s\n' started >> "$events" ;;
    run)
      printf '%s\n' control-cli >> "$events"
      local expected_mount="type=bind,src=${expected_state_path%/*},dst=${expected_state_path%/*}"
      local actual_mount=false arg
      for arg in "$@"; do [ "$arg" != "$expected_mount" ] || actual_mount=true; done
      [ "$actual_mount" = true ]
      while [ "$1" != findb-fetch-full-market ]; do shift; done
      shift
      "$fetcher_python" "$control_cli" "$@"
      ;;
    *) return 96 ;;
  esac
}
"""
    retire = re.search(r"retire_runtime\(\) \{\n.*?\n\}", deploy, re.S).group()
    registration = "\n".join(
        re.search(rf"{name}\(\) \{{\n.*?\n\}}", deploy, re.S).group()
        for name in ("register_named_full", "register_provider", "registered_original")
    )
    shell += registration + "\n" + retirement + "\n" + move + "\n" + retire + "\n"
    shell += (
        r"""run_runtime() {
  printf '%s\n' runtime-secret-wrapper >> "$events"
  [ "$FETCHER_RUNTIME_PROFILE" = full-market ]
  [ "$FETCHER_RECORDED_RELEASE_PROFILE" = "$recorded_profile" ]
  [ "$FETCHER_CONSUMER_PROFILE" = full_market ]
  while [ "$1" != -- ]; do shift; done
  shift 2
  provider="$1"; image="$2"; stable="$5"; previous="$7"; preflight_name="$8"
  if [ "$recorded_profile" = bounded ] && [ "$FETCHER_ACCEPTED_REPLAY" = true ]; then
    [ "$image" = installed-compatible-image ]
  fi
  [ "$3" = "${expected_state_path%/*}" ]
  [ "$4" = "$expected_state_path" ]
  state_dir="$3"; state_path="$4"
  export FETCHER_STATE_PATH="$state_path"
  shift 9
  cache_mount=(--mount fixture-cache)
  account_mount=()
  readiness_mount=()
  runtime_env_args=(--env APP_ENVIRONMENT)
"""
        + helper[
            helper.index('if runtime_exists "$previous"') : helper.index("\ndocker image prune")
        ]
        + bootstrap
        + control
        + "\n}\n"
        + coordinator
    )
    result = subprocess.run(
        ["/bin/bash", "-c", shell],
        env={
            **os.environ,
            "APP_ENVIRONMENT": target,
            "expected_state_path": "/var/lib/findb-full-market"
            + ("/staging" if target == "staging" else "")
            + "/state.sqlite3",
            "probe_paths": str(tmp_path / "probe-paths"),
            "FETCHER_RUNTIME_PROFILE": profile,
            "FETCHER_ACCEPTED_REPLAY": str(replay).lower(),
            "checkpoint": str(checkpoint),
            "containers": str(containers),
            "events": str(events),
            "control_cli": str(cli),
            "fetcher_python": str(REPO_ROOT / "fetcher/.venv/bin/python"),
            "repo": str(REPO_ROOT),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    if not existing:
        assert result.returncode != 0
        reason = "missing" if state_kind == "missing" else "invalid"
        assert f"reason=full_market_checkpoint_{reason}" in result.stderr
        if state_kind == "missing":
            assert not checkpoint.parent.exists()
        else:
            assert checkpoint.read_bytes() == original_checkpoint
        assert not events.exists()  # No rename, initializer, secret wrapper, Source or provider.
        assert original.stat().st_ino == original_inode
        assert all(path.stat().st_ino == inode for path, inode in historical)
        expected = (
            "/var/lib/findb-full-market"
            + ("/staging" if target == "staging" else "")
            + "/state.sqlite3"
        )
        paths = (tmp_path / "probe-paths").read_text().splitlines()
        assert paths == (
            [expected]
            if state_kind == "missing"
            else [expected, expected, expected.rsplit("/", 1)[0]]
        )
    else:
        assert result.returncode == 0, result.stderr
        assert checkpoint.read_bytes() == original_checkpoint
        observed = events.read_text().splitlines()
        assert observed == ["renamed"] * 3 + (["renamed"] if legacy_name else []) + [
            "runtime-secret-wrapper"
        ] + (["renamed", "started"] if previous_only else []) + [
            "control-cli",
            "Source-control",
        ] + [
            "runtime-secret-wrapper",
            "control-cli",
            "Source-control",
        ] * (3 if profile == "full-market" else 0)
        assert (containers / "findb-full-market-twelve-data").stat().st_ino == original_inode
        with sqlite3.connect(checkpoint) as db:
            assert db.execute("SELECT body FROM full_work").fetchone()[0] == b"prepared"
            assert db.execute("SELECT bytes FROM full_quota").fetchone()[0] == 250
            assert db.execute("SELECT trade_date FROM full_cursor").fetchone()[0] == "2026-10-01"


@pytest.mark.parametrize(
    "original_name,stale_running",
    [("stable", None), ("stable", False), ("stable", True), ("previous", None)],
)
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize(
    "boundary",
    [
        "before-helper",
        "after-recovery",
        "after-retirement",
        "after-replacement",
        "signal-rename",
        "signal-stop",
        "signal-replacement",
        "stop-failure",
        "persistent-stop-failure",
        "retirement-failure",
        "candidate",
        "commit",
    ],
)
def test_named_full_journal_restores_names_state_and_stale_backup(
    tmp_path, original_name, running, stale_running, boundary
):
    deploy = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    transaction = (
        _inventory_policy(deploy)
        + deploy.split("processed=()", 1)[1].split("retire_runtime() {", 1)[0]
    )
    restore = helper[
        helper.index('if runtime_exists "$previous"') : helper.index("\ndocker image prune")
    ]
    retirement = helper[
        helper.index("had_previous=0\n") : helper.index("# The deployment identity only reports")
    ]
    snapshot = helper[helper.rindex("recovery_original_id=-\n") : helper.index("\nrecover() {")]
    state = tmp_path / "containers"
    state.mkdir()
    stable = "findb-full-market-finlab"
    previous = stable + "-previous"
    original = state / (stable if original_name == "stable" else previous)
    original.write_text("running\n" if running else "stopped\n")
    original_inode = original.stat().st_ino
    backup = state / previous
    stale_inode = None
    if stale_running is not None:
        backup.write_text("running\n" if stale_running else "stopped\n")
        stale_inode = backup.stat().st_ino
    checkpoint = tmp_path / "state.sqlite3"
    checkpoint.write_bytes(b"prepared payload debt and cursors")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake = _FAKE_DOCKER
    if boundary == "stop-failure":
        fake = fake.replace(
            'name="$3"',
            'name="$3"\n    if [ ! -e "$state_dir/stop-failed" ]; then touch "$state_dir/stop-failed"; exit 1; fi',
            1,
        )
    elif boundary == "persistent-stop-failure":
        fake = fake.replace('name="$3"', 'name="$3"\n    exit 1', 1)
    (binaries / "docker").write_text(fake)
    (binaries / "docker").chmod(0o700)
    operations = 'register_provider "$stable" "$previous"\n'
    if boundary == "before-helper":
        operations += "false\n"
    else:
        operations += restore + "\n"
        if boundary == "after-recovery":
            operations += "false\n"
        else:
            operations += snapshot + "\n"
            if boundary in {"stop-failure", "persistent-stop-failure"} and not running:
                # A stopped stable needs no normal stop. Model an external resume
                # so the refusal occurs at the actual retirement stop boundary.
                operations += 'printf "running\\n" > "$FAKE_DOCKER_STATE/$stable"\n'
            operations += retirement + "\n"
            if boundary == "after-retirement":
                operations += "false\n"
            else:
                operations += 'printf "running\\n" > "$FAKE_DOCKER_STATE/$stable"\n'
                if boundary in {"after-replacement", "signal-replacement"}:
                    operations += (
                        "kill -TERM $$\n" if boundary == "signal-replacement" else "false\n"
                    )
                elif boundary == "commit":
                    operations += 'prepare_named_full_backup_cleanup\ntrap - ERR INT TERM HUP\ndocker rm "$previous"\ncleanup_named_full_backups\n'
                else:
                    operations += "rollback_processed\ntrap - ERR INT TERM HUP\n"
    shell = f"set -Eeuo pipefail\nFETCHER_DEPLOY_MODE=candidate\nprocessed=(){transaction}\nstable={stable}\nprevious={previous}\n{operations}"
    env = {
        **os.environ,
        "PATH": str(binaries) + ":" + os.environ["PATH"],
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_IMAGE": "accepted-image@sha256:fixture",
    }
    if boundary in {"signal-rename", "signal-stop"}:
        env.update(
            FAKE_SIGNAL_ON=boundary.split("-")[1], FAKE_SIGNAL_SENT=str(tmp_path / "signal-sent")
        )
    if boundary == "retirement-failure":
        env["FAKE_STABLE_EXIT_CODE"] = "1"
    result = subprocess.run(
        ["/bin/bash", "-c", shell], env=env, capture_output=True, text=True, timeout=10
    )
    assert checkpoint.read_bytes() == b"prepared payload debt and cursors"
    if boundary == "commit":
        assert result.returncode == 0, result.stderr
        assert (state / stable).stat().st_ino != original_inode
        assert not backup.exists()
        assert not (state / (stable + "-transaction-backup")).exists()
    else:
        assert (result.returncode == 0) == (boundary == "candidate"), result.stderr
        assert original.stat().st_ino == original_inode, result.stderr
        if boundary == "persistent-stop-failure" and not running:
            # External permanent refusal cannot restore stopped state. Fail
            # explicitly, retain identity, and never force-remove the original.
            assert "reason=transaction_rollback_failed" in result.stderr
            assert original.read_text().strip() == "running"
            assert "rm " not in (tmp_path / "docker.log").read_text()
        else:
            assert original.read_text().strip() == ("running" if running else "stopped")
        if stale_inode is not None:
            assert backup.stat().st_ino == stale_inode
            assert backup.read_text().strip() == ("running" if stale_running else "stopped")
        elif original_name == "stable":
            assert not backup.exists()
        else:
            assert not (state / stable).exists()
        assert not (state / (stable + "-transaction-backup")).exists()


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("previous_only", [False, True])
@pytest.mark.parametrize(
    "boundary",
    [
        "stable-existence-1",
        "previous-existence-1",
        "stable-existence-81",
        "previous-existence-81",
        "backup-existence-1",
        "backup-existence-81",
        "backup-list-1",
        "backup-list-81",
        "current-label",
        "current-id",
        "current-image",
        "current-running",
        "stale-label",
        "stale-id",
        "stale-image",
        "stale-running",
        "collision",
        "signal-original-journal",
        "signal-backup-journal",
        "signal-processed",
        "signal-staged",
    ],
)
def test_named_registration_preflight_and_each_journal_boundary_preserve_originals(
    tmp_path, running, previous_only, boundary
):
    source = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text()
    transaction = (
        _inventory_policy(source)
        + source.split("processed=()", 1)[1].split("retire_runtime() {", 1)[0]
    )
    state = tmp_path / "containers"
    state.mkdir()
    stable = "findb-full-market-finlab"
    previous = stable + "-previous"
    original = state / (previous if previous_only else stable)
    original.write_text("running\n" if running else "stopped\n")
    stale = None
    if not previous_only:
        stale = state / previous
        stale.write_text("stopped\n" if running else "running\n")
    collision = state / (stable + "-transaction-backup")
    if boundary == "collision" and not previous_only:
        collision.write_text("stopped\n")
    before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in state.iterdir()}
    fake = _FAKE_DOCKER.replace('"$1" >> "${FAKE_DOCKER_LOG:?}"', '"$*" >> "${FAKE_DOCKER_LOG:?}"')
    if "existence" in boundary:
        which, _, rc = boundary.split("-")
        selected = {"stable": stable, "previous": previous, "backup": collision.name}[which]
        if which == "backup" and not previous_only:
            collision.write_text("stopped\n")
            before = {p.name: (p.stat().st_ino, p.read_bytes()) for p in state.iterdir()}
        fake = fake.replace(
            '    [ "$1" = inspect ]',
            f'    [ "$1" = inspect ]\n    [ "$2" != "{selected}" ] || exit {rc}',
        )
    if boundary.startswith("backup-list-"):
        rc = boundary.rsplit("-", 1)[1]
        fake = fake.replace(
            '    if [ "$1" = ls ]; then',
            f'    if [ "$1" = ls ]; then\n      count=0; [ ! -f "$FAKE_QUERY_COUNT" ] || count=$(cat "$FAKE_QUERY_COUNT")\n      count=$((count + 1)); printf "%s\\n" "$count" > "$FAKE_QUERY_COUNT"\n      [ "$count" -ne 3 ] || exit {rc}',
        )
    if boundary.startswith(("current-", "stale-")):
        prefix, field = boundary.split("-")
        selected = original.name if prefix == "current" else previous
        # In previous-only cases stale inspect boundaries are checked against
        # that actual original too; every failure must precede journal publication.
        pattern = {
            "label": "Config.Labels",
            "id": "{{.Id}}",
            "image": "Config.Image",
            "running": "State.Running",
        }[field]
        fake = fake.replace(
            'format="$2"\n    name="$3"',
            f'format="$2"\n    name="$3"\n    if [ "$name" = "{selected}" ]; then case "$format" in *"{pattern}"*) exit 81 ;; esac; fi',
            1,
        )
    markers = {
        "signal-original-journal": '  if [ "$backup" != - ]; then named_full_backups+=',
        "signal-backup-journal": '  processed+=("${stable}:${previous}:${old_available}:${original_id}")',
        "signal-processed": '  if [ "$backup" != - ]; then docker rename',
        "signal-staged": "\n}\nrestore_named_identity()",
    }
    if boundary in markers:
        marker = markers[boundary]
        assert marker in transaction
        transaction = transaction.replace(marker, "\n  kill -TERM $$\n" + marker, 1)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text(fake)
    (binaries / "docker").chmod(0o700)
    shell = f"set -Eeuo pipefail\nFETCHER_DEPLOY_MODE=candidate\nprocessed=(){transaction}\nregister_provider {stable} {previous}\nfalse\n"
    result = subprocess.run(
        ["/bin/bash", "-c", shell],
        env={
            **os.environ,
            "PATH": str(binaries) + ":" + os.environ["PATH"],
            "FAKE_DOCKER_STATE": str(state),
            "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
            "FAKE_QUERY_COUNT": str(tmp_path / "query-count"),
            "FAKE_IMAGE": "accepted-image@sha256:fixture",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert before == {p.name: (p.stat().st_ino, p.read_bytes()) for p in state.iterdir()}, (
        result.stderr
    )
    assert "rm " not in (tmp_path / "docker.log").read_text()


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("location", ["candidate", "stable"])
@pytest.mark.parametrize("trigger", ["health-failure", "INT", "TERM", "HUP"])
@pytest.mark.parametrize("retirement", ["clean", "stop-refused", "exit-137", "oom", "docker-error"])
def test_actual_helper_recovery_traps_gracefully_retire_or_preserve_runtime(
    tmp_path, running, location, trigger, retirement
):
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    recovery = (
        _inventory_policy(helper)
        + helper[
            helper.index("remove_recovery_runtime() {") : helper.index(
                '\nif runtime_exists "$candidate"; then'
            )
        ]
        + helper[
            helper.index(
                "recovery_original_id=-\n", helper.index('"$image" "$@" --check')
            ) : helper.index("had_previous=0\n")
        ]
    )
    state = tmp_path / "containers"
    state.mkdir()
    stable = "findb-full-market-finlab"
    previous = stable + "-previous"
    candidate = stable + "-candidate"
    original = state / stable
    original.write_text("running\n" if running else "stopped\n")
    original_inode = original.stat().st_ino
    fake = _FAKE_DOCKER.replace('"$1" >> "${FAKE_DOCKER_LOG:?}"', '"$*" >> "${FAKE_DOCKER_LOG:?}"')
    victim = candidate if location == "candidate" else stable
    if retirement == "stop-refused":
        fake = fake.replace('name="$3"', f'name="$3"\n    [ "$name" != "{victim}" ] || exit 82', 1)
    if retirement in {"exit-137", "oom", "docker-error"}:
        pattern, value = {
            "exit-137": ("State.ExitCode", "137"),
            "oom": ("State.OOMKilled", "true"),
            "docker-error": ("State.Error", "host detail must remain private"),
        }[retirement]
        fake = fake.replace(
            'format="$2"\n    name="$3"',
            f'format="$2"\n    name="$3"\n    if [ "$name" = "{victim}" ]; then case "$format" in *"{pattern}"*) printf "%s\\n" "{value}"; exit 0 ;; esac; fi',
            1,
        )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text(fake)
    (binaries / "docker").chmod(0o700)
    # Actual helper trap registration precedes the same original->previous
    # transition. The running candidate/stable represents the unhealthy worker.
    shell = (
        f'set -Eeuo pipefail\nstable={stable}\nprevious={previous}\ncandidate={candidate}\n{recovery}\ndocker rename "$stable" "$previous"\nprintf "running\\n" > "$FAKE_DOCKER_STATE/{victim}"\n'
        + ("false\n" if trigger == "health-failure" else f"kill -{trigger} $$\n")
    )
    result = subprocess.run(
        ["/bin/bash", "-c", shell],
        env={
            **os.environ,
            "PATH": str(binaries) + ":" + os.environ["PATH"],
            "FAKE_DOCKER_STATE": str(state),
            "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
            "FAKE_QUERY_COUNT": str(tmp_path / "query-count"),
            "FAKE_IMAGE": "accepted-image@sha256:fixture",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    log = (tmp_path / "docker.log").read_text().splitlines()
    assert not any(line.startswith("rm -f") for line in log)
    stop = next(i for i, line in enumerate(log) if line == f"stop --time 30 {victim}")
    if retirement == "clean":
        removal = next(i for i, line in enumerate(log) if line == f"rm {victim}")
        assert stop < removal
        assert original.stat().st_ino == original_inode
        assert original.read_text().strip() == ("running" if running else "stopped")
        assert not (state / candidate).exists()
        assert not (state / previous).exists()
    else:
        assert f"rm {victim}" not in log
        assert "reason=recovery_failed" in result.stderr
        assert "host detail must remain private" not in result.stderr
        assert (state / previous).stat().st_ino == original_inode
        assert (state / victim).exists()
