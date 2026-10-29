"""Actual Docker arguments and legacy-name transaction recovery, entirely offline."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from script_env import script_env

REPO_ROOT = Path(__file__).resolve().parents[2]
_FAKE_DOCKER = (REPO_ROOT / "infra/tests/fixtures/fake_docker.sh").read_text(encoding="utf-8")


def _inventory_policy(source):
    start = source.index("# A failed inspect is not proof of absence:")
    end = source.index('\n: "${', start)
    return "\n" + source[start:end] + "\n"


def test_provider_stop_timeout_covers_bounded_factset_query_and_reap():
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()

    assert "provider_stop_timeout_seconds=90" in helper
    assert helper.count('docker stop --time "$provider_stop_timeout_seconds"') == 3
    assert "docker stop --time 30" not in helper


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
    names = {
        "twelve_data": "findb-pilot-twelve-data",
        "finlab": "findb-pilot-finlab",
        "shioaji": "findb-pilot-shioaji",
    }
    governor = "/var/lib/findb-account/production/governor.sqlite3"
    image = "image@sha256:" + "b" * 64
    manifest = {
        "deployment_target": "production",
        "runtime_profile": "unified",
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
            else "findb-historical-" + provider.replace("_", "-")
            if consumer == "historical"
            else names[provider]
        )
        args_env = {
            **script_env(),
            "APP_ENVIRONMENT": "production",
            "FULL_MARKET_ENABLED": "true",
            "FETCHER_CONSUMER_PROFILE": consumer,
            "FETCHER_ACCOUNT_STATE_PATH": governor,
            "SOURCE_CLIENT_KEY": "source-fixture",
            "FETCHER_HISTORICAL_STATE_DIR": "/checkpoint",
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
                f"set -- findb-fetch-runtime --execution-profile {consumer} --config /installed/config",
                'docker() { printf "%s\\n" "$@"; }',
                environment_args,
                common,
                normal,
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


@pytest.mark.parametrize("target", ["local", "staging", "production"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("provider", ["twelve_data", "finlab"])
@pytest.mark.parametrize("consumer", ["full_market", "pilot"])
def test_actual_full_state_bootstrap_forwards_only_canonical_environment(
    tmp_path, target, existing, provider, consumer
):
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text()
    start = helper.index('case "${FETCHER_CONSUMER_PROFILE:-}:$1" in')
    bootstrap = helper[start:].split('\ndocker run --rm \\\n  --name "$preflight_name"', 1)[0]
    state = tmp_path / "state.sqlite3"
    log = tmp_path / "docker-args"
    if existing:
        state.write_bytes(b"existing checkpoint must not be rewritten")
    script = (
        """set -euo pipefail
stable=stable
previous=previous
preflight_name=preflight
image=reviewed-image
cache_mount=()
account_mount=()
account_dir="$state_dir/account"
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
    command = [
        "findb-fetch-runtime",
        "--execution-profile",
        consumer,
        "--config",
        str(config),
        "--provider",
        provider,
    ]
    environment = script_env()
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
    if existing:
        assert not log.exists()
        assert state.read_bytes() == b"existing checkpoint must not be rewritten"
        return
    arguments = log.read_text().splitlines()
    environment = {}
    for index, argument in enumerate(arguments[:-1]):
        if argument == "--env":
            key, separator, value = arguments[index + 1].partition("=")
            assert separator
            environment[key] = value
    assert environment == {
        "APP_ENVIRONMENT": target,
        "FETCHER_ACCOUNT_STATE_PATH": str(tmp_path / "account/governor.sqlite3"),
    }
    assert "SOURCE_SECRET=must-not-be-injected" not in arguments
    state.unlink()  # Execute the real CLI with exactly the extracted Docker environment.
    cli = arguments[arguments.index("findb-fetch-runtime") + 1 :]
    completed = subprocess.run(
        # -I: Fetcher (an editable dev dependency) imports; backend `app` does not.
        [sys.executable, "-I", "-m", "findb_fetcher.full_market_cli", *cli],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert state.read_bytes().startswith(b"SQLite format 3")


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
        + deploy.split("processed=()", 1)[1].split("# A first cutover", 1)[0]
    )
    restore = helper[
        helper.index('if runtime_exists "$previous"') : helper.index(
            '\nif [ "${FETCHER_IMAGES_PREPARED:-false}"'
        )
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
    shell = (
        "set -Eeuo pipefail\n"
        "FETCHER_DEPLOY_MODE=candidate\n"
        "provider_stop_timeout_seconds=90\n"
        f"processed=(){transaction}\nstable={stable}\nprevious={previous}\n{operations}"
    )
    env = {
        **script_env(),
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
        + source.split("processed=()", 1)[1].split("# A first cutover", 1)[0]
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
            **script_env(),
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
        "provider_stop_timeout_seconds=90\n"
        + _inventory_policy(helper)
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
            **script_env(),
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
    stop = next(i for i, line in enumerate(log) if line == f"stop --time 90 {victim}")
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
