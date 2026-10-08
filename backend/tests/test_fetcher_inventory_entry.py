"""Whole current helper entries and parent rollback against an offline Docker API.

The executable helper is unchanged: no injected set -E, alternate traps, or
recovery substitutes. Name inventory and failed inspection are distinct APIs.
"""

import copy
import hashlib
import json
import os
import subprocess
import sys

import pytest

from tests.test_deployment_checks import REPO_ROOT
from tests.test_full_market_deployment_repairs import _inventory_policy

HELPER = REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh"
DEPLOY = REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh"
REGISTRY = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"

FAKE = r"""
import os,sys,json,signal
from pathlib import Path
store=Path(os.environ['STORE']);s=json.loads(store.read_text());a=sys.argv[1:];cmd=Path(sys.argv[0]).name
rows=s['rows'];s['log'].append([cmd,*a]);status=0;output=None
if cmd=='sudo':
    if a[0]=='stat':output='0:0:755' if 'readiness' in a[-1] else ('10001:10001:600' if Path(a[-1]).is_file() else '10001:10001:700')
    elif a[0]=='test':status=0 if Path(a[-1]).exists() else 1
    elif a[0]=='cat':output=Path(a[-1]).read_text().strip()
    elif a[0] in ['mkdir','chown','chmod']:pass
    else:status=91
elif cmd=='sleep':pass
elif cmd=='docker':
    if a[:2]==['container','ls']:
        if s.get('inventory_failure'): status=s['failure_rc'];output='private daemon error'
        else:output='\n'.join(rows)
    elif a[:2]==['container','inspect']:
        name=a[-1]
        if s.get('exist_fail')==name and not s.get('exist_failed'):
            s['exist_failed']=True;status=s.get('failure_rc',81);output='private daemon error'
        else:status=0 if name in rows else 1
    elif a[0]=='inspect':
        name=a[-1];fmt=a[2];row=rows.get(name)
        if row is None:status=1
        else:
            fields={'{{.Id}}':row['id'],'{{.Config.Image}}':row['image'],'{{.State.Running}}':str(row['running']).lower(),'{{.State.ExitCode}}':str(row.get('exit',0)),'{{.State.OOMKilled}}':str(row.get('oom',False)).lower(),'{{.State.Error}}':row.get('error',''),'{{.State.Status}}':'running' if row['running'] else 'exited','{{.Config.User}}':'10001:10001','{{.RestartCount}}':'0','{{.HostConfig.RestartPolicy.Name}}':'no'}
            output=row.get('accepted','true') if 'Config.Labels' in fmt else (json.dumps(row['command'],separators=(',',':')) if fmt=='{{json .Config.Cmd}}' else fields.get(fmt))
            if output is None:status=90
            if s.get('recovery_query_active') and fmt=='{{.State.Running}}' and row['id']=='original-current':
                failure=s.get('recovery_running_failure')
                s['recovery_query_count']=s.get('recovery_query_count',0)+1
                if failure in ['rc1','rc81']:status=int(failure[2:]);output=None
                elif failure=='empty':output=''
                elif failure=='invalid':output='unverified'
            if s.get('recovery_query_active') and os.environ.get('COORDINATOR_QUERY_TEST')=='1' and fmt==s.get('recovery_identity_field') and row['id']=='original-current':
                s['identity_query_count']=s.get('identity_query_count',0)+1
                status=s['failure_rc'] # Deliberately retain valid matching stdout.
            if s.get('normal_retirement_active') and row['id']=='original-current' and fmt==s.get('normal_field'):
                failure=s['normal_failure']
                if failure in ['rc1','rc81']:status=int(failure[2:]);output=None
                elif failure=='empty':output=''
                elif failure=='invalid':output='unverified'
            victim=s.get('victim')
            if name==victim and (row['id'].startswith('new-') or row['id']=='interrupted-candidate'):
                if fmt=='{{.Config.Image}}' and s['trigger']=='health-failure':output='wrong-image'
                if fmt=='{{.State.ExitCode}}' and s['retirement']=='exit-137':output='137'
                if fmt=='{{.State.OOMKilled}}' and s['retirement']=='oom':output='true'
                if fmt=='{{.State.Error}}' and s['retirement']=='docker-error':output='private host error'
    elif a[0]=='stop':
        name=a[-1]
        if name==s.get('victim') and (rows[name]['id'].startswith('new-') or rows[name]['id']=='interrupted-candidate') and s['retirement']=='stop-refused':status=82
        else:rows[name]['running']=False;rows[name]['exit']=0
    elif a[0]=='start':rows[a[-1]]['running']=True
    elif a[0]=='rm':
        name=a[-1]
        if name not in rows:status=1
        elif '-f' not in a and rows[name]['running']:status=83
        else:del rows[name]
    elif a[0]=='rename':
        old,new=a[1:3]
        if new in rows:status=84
        else:
            rows[new]=rows.pop(old)
            if new==s.get('victim') and rows[new]['id'].startswith('new-') and s['trigger'] in ['INT','TERM','HUP']:s['send']=s['trigger']
    elif a[0] in ['image','pull']:pass
    elif a[0] in ['run','create']:
        if '-d' in a or a[0]=='create':
            name=a[a.index('--name')+1]
            if name in rows:status=85
            else:
                rows[name]={'id':'new-'+name,'image':s['new_image'],'running':a[0]=='run','accepted':'true'}
                if name==s.get('victim') and s['trigger'] in ['INT','TERM','HUP']:s['send']=s['trigger']
        else:
            if s['trigger']=='preflight-failure' and '--check' in a:status=87;s['recovery_query_active']=True
            elif '--check' in a and s.get('normal_field'):s['normal_retirement_active']=True
    else:status=89
else:status=88
if cmd=='docker' and a[0]=='inspect' and output=='wrong-image':s['recovery_query_active']=True
send=s.pop('send',None)
if s.get('outage_on_recovery') and (send or (cmd=='docker' and a[0]=='inspect' and output=='wrong-image')):
    s['inventory_failure']=True
store.write_text(json.dumps(s))
if output is not None:print(output)
if send:os.kill(os.getppid(),getattr(signal,'SIG'+send))
raise SystemExit(status)
"""


def run_entry(
    tmp_path,
    *,
    running=False,
    location="candidate",
    trigger="preflight-failure",
    retirement="clean",
    interrupted=False,
    parent=False,
    exist_fail=None,
    failure_rc=81,
    inventory_failure=False,
    stale=False,
    installed=True,
    previous_only=False,
    entry="helper",
    outage_on_recovery=False,
    recovery_running_failure=None,
    recovery_identity_field=None,
    kind="full",
    original_fields=None,
    normal_field=None,
    normal_failure=None,
):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    executable = binaries / "fake"
    executable.write_text(f"#!{sys.executable}\n" + FAKE)
    executable.chmod(0o700)
    for name in ["docker", "sudo", "sleep"]:
        (binaries / name).symlink_to(executable)
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    durable = checkpoint / "state.sqlite3"
    durable.write_bytes(b"prepared-payload-debt-and-cursors")
    marker_identity = "full-market" if kind == "full" else "twelve"
    fingerprint = hashlib.sha256(("a" * 32 + "\nraw-data\n" + marker_identity).encode()).hexdigest()
    (checkpoint / "raw-bucket.sha256").write_text(fingerprint + "\n")
    stable = "findb-full-market-twelve-data" if kind == "full" else "findb-fetcher-scheduler"
    if kind == "historical":
        stable += "-historical"
    previous = stable + "-previous"
    candidate = stable + "-candidate"
    image = REGISTRY + "/findb/production/fetcher/twelve-data@sha256:" + "b" * 64
    runtime_command = (
        "findb-fetch-full-market"
        if kind == "full"
        else (
            "findb-fetch-historical-backfill" if kind == "historical" else "findb-fetch-scheduler"
        )
    )
    rows = {
        stable: dict(
            id="original-current",
            running=running,
            image=image,
            accepted="true",
            exit=0,
            command=[runtime_command],
        )
    }
    if stale:
        rows[previous] = dict(
            id="original-stale",
            running=not running,
            image=image,
            accepted="true",
            exit=0,
            command=[runtime_command],
        )
    if interrupted:
        rows[candidate] = dict(
            id="interrupted-candidate",
            running=True,
            image=image,
            accepted="false",
            exit=0,
            command=[runtime_command],
        )
    if not installed:
        rows = {}
    elif previous_only:
        rows[previous] = rows.pop(stable)
    if original_fields:
        rows[previous if previous_only else stable].update(original_fields)
    before = copy.deepcopy(rows)
    victim = candidate if location == "candidate" else stable
    state = tmp_path / "daemon.json"
    state.write_text(
        json.dumps(
            dict(
                rows=rows,
                log=[],
                victim=victim,
                trigger=trigger,
                retirement=retirement,
                new_image=image,
                exist_fail=exist_fail,
                failure_rc=failure_rc,
                inventory_failure=inventory_failure,
                outage_on_recovery=outage_on_recovery,
                recovery_running_failure=recovery_running_failure,
                recovery_identity_field=recovery_identity_field,
                normal_field=normal_field,
                normal_failure=normal_failure,
            )
        )
    )
    env = {
        **os.environ,
        "PATH": str(binaries) + ":" + os.environ["PATH"],
        "STORE": str(state),
        "AWS_REGION": "ap-southeast-1",
        "AWS_ACCOUNT_ID": "439622209937",
        "APP_ENVIRONMENT": "production",
        "ECR_REGISTRY": REGISTRY,
        "FETCHER_PROVIDER_RELEASE_MODE": "transactional",
        "FETCHER_DEPLOY_MODE": "activate",
        "FETCHER_RUNTIME_PROFILE": "full-market",
        "FETCHER_CONSUMER_PROFILE": "full_market"
        if kind == "full"
        else ("historical" if kind == "historical" else "pilot"),
        "FULL_MARKET_ENABLED": "false",
        "FETCHER_SOURCE_API_URL": "https://example.invalid/source",
        "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY": "source-fixture",
        "TWELVE_DATA_API_KEY": "provider-fixture",
        "FINDB_SERVE_BASE_URL": "https://example.invalid/serve",
        "FETCHER_CALENDAR_SERVE_API_KEY": "calendar-fixture",
        "CLOUDFLARE_R2_ACCOUNT_ID": "a" * 32,
        "CLOUDFLARE_R2_RAW_BUCKET": "raw-data",
        "CLOUDFLARE_R2_RAW_ACCESS_KEY_ID": "access-fixture",
        "CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY": "secret-fixture",
    }
    args = [
        str(HELPER),
        "twelve-data",
        image,
        str(checkpoint),
        str(durable),
        stable,
        candidate,
        previous,
        stable + "-preflight",
        "-",
        "findb-fetch-full-market"
        if kind == "full"
        else (
            "findb-fetch-historical-backfill" if kind == "historical" else "findb-fetch-scheduler"
        ),
    ]
    if kind == "historical":
        args.extend(["--provider", "twelve_data", "--run-forever"])
    command = ["/bin/bash", *args]
    if parent:
        source = DEPLOY.read_text()
        transaction = source.split("processed=()", 1)[1].split("retire_runtime() {", 1)[0]
        # Execute actual registration/rollback together with the whole child.
        script = (
            "set -Eeuo pipefail\nFETCHER_DEPLOY_MODE=candidate\nprocessed=()\n"
            + _inventory_policy(source)
            + transaction
        )
        script += f'\nexport COORDINATOR_QUERY_TEST=1\nregister_provider {stable} {previous}\nCOORDINATOR_QUERY_TEST= /bin/bash "$@"\n: \n'
        command = ["/bin/bash", "-c", script, "entry", *args]
    if entry == "guard":
        command = ["/bin/bash", str(REPO_ROOT / "infra/deploy/check_full_market_checkpoint.sh")]
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    actual = json.loads(state.read_text())
    assert durable.read_bytes() == b"prepared-payload-debt-and-cursors"
    assert not any(a[:3] == ["docker", "rm", "-f"] for a in actual["log"])
    assert "private daemon error" not in completed.stderr
    return completed, before, actual


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("target", ["stable", "previous", "inventory"])
@pytest.mark.parametrize("failure_rc", [1, 81])
@pytest.mark.parametrize("parent", [False, True])
def test_whole_helper_inventory_failures_preserve_initial_identities(
    tmp_path, running, target, failure_rc, parent
):
    name = "findb-full-market-twelve-data" + ("-previous" if target == "previous" else "")
    result, before, actual = run_entry(
        tmp_path,
        running=running,
        parent=parent,
        exist_fail=name if target != "inventory" else None,
        inventory_failure=target == "inventory",
        failure_rc=failure_rc,
        stale=True,
    )
    assert result.returncode != 0
    assert actual["rows"] == before
    assert "reason=query_unknown" in result.stderr
    assert not any(
        a[0] == "docker" and a[1] in {"stop", "start", "rename", "rm", "run", "create"}
        for a in actual["log"]
    )


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("retirement", ["clean", "stop-refused", "exit-137", "oom", "docker-error"])
def test_whole_helper_interrupted_running_candidate_entry(tmp_path, running, retirement):
    result, before, actual = run_entry(
        tmp_path, running=running, interrupted=True, retirement=retirement
    )
    assert result.returncode != 0
    stable = "findb-full-market-twelve-data"
    assert actual["rows"][stable] == before[stable]
    actions = actual["log"]
    stop = next(i for i, a in enumerate(actions) if a[:2] == ["docker", "stop"])
    if retirement == "clean":
        removal = next(i for i, a in enumerate(actions) if a[:2] == ["docker", "rm"])
        assert stop < removal
        assert stable + "-candidate" not in actual["rows"]
    else:
        assert actual["rows"][stable + "-candidate"]["id"] == "interrupted-candidate"
        assert not any(a[:2] == ["docker", "rm"] for a in actions)


@pytest.mark.parametrize(
    "location,trigger",
    [
        ("candidate", "health-failure"),
        ("candidate", "TERM"),
        ("candidate", "INT"),
        ("candidate", "HUP"),
        ("stable", "TERM"),
        ("stable", "INT"),
        ("stable", "HUP"),
    ],
)
@pytest.mark.parametrize("retirement", ["clean", "stop-refused"])
@pytest.mark.parametrize("running", [False, True])
def test_whole_helper_actual_traps_preserve_original_and_retire_candidate(
    tmp_path, location, trigger, retirement, running
):
    result, before, actual = run_entry(
        tmp_path, location=location, trigger=trigger, retirement=retirement, running=running
    )
    assert result.returncode != 0
    stable = "findb-full-market-twelve-data"
    if retirement == "clean":
        assert actual["rows"][stable] == before[stable]
        assert len(actual["rows"]) == 1
    else:
        assert "reason=recovery_failed" in result.stderr
        assert any(r["id"].startswith("new-") for r in actual["rows"].values())


@pytest.mark.parametrize("target", ["stable", "previous", "inventory"])
@pytest.mark.parametrize("failure_rc", [1, 81])
def test_external_guard_unknown_inventory_never_skips_checkpoint_gate(tmp_path, target, failure_rc):
    name = "findb-full-market-twelve-data" + ("-previous" if target == "previous" else "")
    result, before, actual = run_entry(
        tmp_path,
        entry="guard",
        previous_only=target == "previous",
        failure_rc=failure_rc,
        exist_fail=name if target != "inventory" else None,
        inventory_failure=target == "inventory",
    )
    assert result.returncode != 0
    assert actual["rows"] == before
    assert "reason=query_unknown" in result.stderr
    if target != "inventory":
        assert actual["exist_failed"]
    assert not any(a[0] == "sudo" for a in actual["log"])


def test_external_guard_verified_absence_is_genuine_first_install(tmp_path):
    result, before, actual = run_entry(tmp_path, entry="guard", installed=False)
    assert result.returncode == 0, result.stderr
    assert actual["rows"] == before == {}
    assert not any(a[0] == "sudo" for a in actual["log"])


@pytest.mark.parametrize("running", [False, True])
def test_previous_only_whole_parent_and_child_restore_exact_original(tmp_path, running):
    result, before, actual = run_entry(tmp_path, parent=True, previous_only=True, running=running)
    assert result.returncode != 0
    assert actual["rows"] == before


def test_whole_helper_known_absence_remains_new_install(tmp_path):
    result, before, actual = run_entry(tmp_path, installed=False, trigger="success")
    assert result.returncode == 0, result.stderr
    assert before == {}
    assert list(actual["rows"]) == ["findb-full-market-twelve-data"]


def test_whole_helper_unstaged_accepted_previous_is_protected(tmp_path):
    result, before, actual = run_entry(tmp_path, stale=True)
    assert result.returncode != 0
    assert "reason=accepted_previous_unjournaled" in result.stderr
    assert actual["rows"] == before
    assert not any(
        a[0] == "docker" and a[1] in {"stop", "start", "rm", "rename", "run", "create"}
        for a in actual["log"]
    )


@pytest.mark.parametrize("trigger", ["health-failure", "TERM"])
def test_whole_helper_unknown_inventory_during_recovery_preserves_identities(tmp_path, trigger):
    result, before, actual = run_entry(tmp_path, trigger=trigger, outage_on_recovery=True)
    assert result.returncode != 0
    assert "reason=recovery_failed" in result.stderr
    originals = [
        row
        for row in actual["rows"].values()
        if row["id"] == before["findb-full-market-twelve-data"]["id"]
    ]
    assert len(originals) == 1
    assert not any(a[:2] == ["docker", "rm"] for a in actual["log"])


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("previous_only", [False, True])
@pytest.mark.parametrize("failure", [None, "rc1", "rc81", "empty", "invalid"])
def test_whole_parent_running_query_must_be_verified_before_restoration(
    tmp_path, running, previous_only, failure
):
    result, before, actual = run_entry(
        tmp_path,
        parent=True,
        running=running,
        previous_only=previous_only,
        recovery_running_failure=failure,
    )
    assert result.returncode != 0
    assert actual.get("recovery_query_count", 0) > 0
    original_name = next(iter(before))
    original = actual["rows"][original_name]
    assert original["id"] == before[original_name]["id"]
    assert original["image"] == before[original_name]["image"]
    assert len(actual["rows"]) == 1
    if failure is None:
        assert original == before[original_name]
        assert "reason=transaction_rollback_failed" not in result.stderr
    else:
        assert "reason=transaction_rollback_failed" in result.stderr
        # Recovery may rename identity, but must not guess start/stop from an
        # unverifiable query. Previous-only helper entry really starts original.
        assert original["running"] == (True if previous_only else running)


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("failure", ["rc1", "rc81", "empty", "invalid"])
def test_whole_helper_running_query_failure_is_explicit_recovery_failure(
    tmp_path, running, failure
):
    result, before, actual = run_entry(
        tmp_path, trigger="health-failure", running=running, recovery_running_failure=failure
    )
    assert result.returncode != 0
    assert actual.get("recovery_query_count", 0) > 0
    assert "reason=recovery_failed" in result.stderr
    original = actual["rows"]["findb-full-market-twelve-data"]
    assert original["id"] == before["findb-full-market-twelve-data"]["id"]
    assert original["image"] == before["findb-full-market-twelve-data"]["image"]
    assert original["running"] is False  # No guessed restart after query failure.


@pytest.mark.parametrize("field", ["Running", "ExitCode", "OOMKilled", "Error"])
@pytest.mark.parametrize("failure", ["rc1", "rc81"])
def test_retirement_field_query_failure_is_not_empty_success(tmp_path, field, failure):
    import re

    source = DEPLOY.read_text()
    function = re.search(r"require_retired_runtime\(\) \{\n.*?\n\}", source, re.S).group()
    # Run under a conditional to deliberately suppress errexit inheritance.
    script = (
        r"""set -euo pipefail
    docker() {
      if [ "$3" = "{{.State.$failed_field}}" ]; then return "$failed_status"; fi
      case "$3" in *Running*|*OOMKilled*) printf '%s\n' false ;; *ExitCode*) printf '%s\n' 0 ;; *Error*) printf '\n' ;; *) return 92 ;; esac
    }
    """
        + function
        + "\nif require_retired_runtime original; then exit 91; fi\n"
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={**os.environ, "failed_field": field, "failed_status": failure[2:]},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("branch", ["legacy", "generic-cleanup"])
@pytest.mark.parametrize("failure", ["rc1", "rc81", "empty", "invalid"])
@pytest.mark.parametrize("running", [False, True])
def test_conditional_legacy_and_generic_recovery_do_not_guess_running_state(
    tmp_path, branch, failure, running
):
    import re

    source = DEPLOY.read_text()
    transaction = source.split("processed=()", 1)[1].split("retire_runtime() {", 1)[0]
    retirement = re.search(r"require_retired_runtime\(\) \{\n.*?\n\}", source, re.S).group()
    fake = r"""
    docker() {
      if [ "$1" = container ]; then
        if [ "$2" = ls ]; then printf '%s\n' original; else [ "$3" = original ]; fi
      elif [ "$1" = inspect ]; then
        case "$3" in
          *State.Running*)
            case "$query_failure" in rc1) return 1 ;; rc81) return 81 ;; empty) printf '\n' ;; invalid) printf '%s\n' unverified ;; esac ;;
          *Config.Image*) printf '%s\n' accepted-image ;;
          *'{{.Id}}'*) printf '%s\n' accepted-id ;;
          *) return 92 ;;
        esac
      else printf '%s\n' "$*" >> "$events"; fi
    }
    """
    operation = (
        f'legacy_moves=("original|full|accepted-id|{str(running).lower()}|accepted-image")\nif rollback_legacy_moves; then exit 91; fi\n'
        if branch == "legacy"
        else "if rollback_provider original previous 0 -; then exit 91; fi\n"
    )
    script = (
        "set -Eeuo pipefail\nFETCHER_DEPLOY_MODE=candidate\nprocessed=()\n"
        + _inventory_policy(source)
        + transaction
        + retirement
        + fake
        + operation
    )
    events = tmp_path / "events"
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={**os.environ, "events": str(events), "query_failure": failure},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not events.exists(), "Unknown state must not select stop/start/remove/rename"


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("previous_only", [False, True])
@pytest.mark.parametrize("failure", [None, "rc1", "rc81", "empty", "invalid"])
def test_generic_whole_parent_helper_restores_exact_initial_state(
    tmp_path, running, previous_only, failure
):
    result, before, actual = run_entry(
        tmp_path,
        kind="generic",
        parent=True,
        running=running,
        previous_only=previous_only,
        recovery_running_failure=failure,
    )
    assert result.returncode != 0
    name = next(iter(before))
    assert actual["rows"][name]["id"] == before[name]["id"]
    assert actual["rows"][name]["image"] == before[name]["image"]
    if failure is None:
        assert actual["rows"] == before
        assert "transaction_rollback_failed" not in result.stderr
    else:
        assert "transaction_rollback_failed" in result.stderr


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("previous_only", [False, True])
@pytest.mark.parametrize("trigger", ["health-failure", "INT", "TERM", "HUP"])
def test_generic_whole_helper_signals_restore_original(tmp_path, running, previous_only, trigger):
    result, before, actual = run_entry(
        tmp_path,
        kind="generic",
        parent=True,
        running=running,
        previous_only=previous_only,
        trigger=trigger,
    )
    assert result.returncode != 0
    assert actual["rows"] == before


@pytest.mark.parametrize(
    "fields", [{"oom": True}, {"error": "private-original-diagnostic"}, {"exit": 137}]
)
@pytest.mark.parametrize("parent", [False, True])
def test_whole_helper_normal_retirement_refuses_unsafe_original(tmp_path, fields, parent):
    result, before, actual = run_entry(
        tmp_path, trigger="success", original_fields=fields, parent=parent
    )
    assert result.returncode != 0
    assert actual["rows"] == before
    assert "private-original-diagnostic" not in result.stderr
    assert not any(row["id"].startswith("new-") for row in actual["rows"].values())


@pytest.mark.parametrize(
    "field,failure",
    [
        (field, failure)
        for field in ["Running", "ExitCode", "OOMKilled", "Error"]
        for failure in ["rc1", "rc81", "empty", "invalid"]
        if (field, failure) != ("Error", "empty")
    ],
)
@pytest.mark.parametrize("parent", [False, True])
def test_whole_helper_normal_retirement_requires_verified_fields(tmp_path, field, failure, parent):
    result, before, actual = run_entry(
        tmp_path,
        trigger="success",
        parent=parent,
        normal_field="{{.State." + field + "}}",
        normal_failure=failure,
    )
    assert result.returncode != 0
    original = next(row for row in actual["rows"].values() if row["id"] == "original-current")
    assert original["image"] == before[next(iter(before))]["image"]
    assert not any(row["id"].startswith("new-") for row in actual["rows"].values())
    assert not any(a[:2] == ["docker", "rm"] for a in actual["log"])


@pytest.mark.parametrize("kind", ["full", "generic", "historical"])
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("parent", [False, True])
def test_every_consumer_preserves_unstaged_accepted_previous(tmp_path, kind, running, parent):
    result, before, actual = run_entry(
        tmp_path, kind=kind, running=running, stale=True, parent=parent
    )
    assert result.returncode != 0
    assert actual["rows"] == before
    if not parent:
        assert "accepted_previous_unjournaled" in result.stderr
        assert not any(call[:2] == ["docker", "rm"] for call in actual["log"])
    else:
        assert "accepted_previous_unjournaled" not in result.stderr
        assert "transaction_rollback_failed" not in result.stderr


@pytest.mark.parametrize("field", ["{{.Id}}", "{{.Config.Image}}"])
@pytest.mark.parametrize("rc", [1, 81])
def test_whole_parent_matching_identity_stdout_requires_success(tmp_path, field, rc):
    result, before, actual = run_entry(
        tmp_path,
        parent=True,
        previous_only=True,
        running=False,
        recovery_identity_field=field,
        failure_rc=rc,
    )
    assert result.returncode != 0
    assert actual.get("identity_query_count", 0) > 0
    assert "transaction_rollback_failed" in result.stderr
    original = next(row for row in actual["rows"].values() if row["id"] == "original-current")
    assert original["image"] == next(iter(before.values()))["image"]
    # The whole child legitimately renamed/started previous before failing.
    # Unverifiable parent identity must not claim to restore its stopped state.
    assert original["running"] is True
    assert not any(call[:2] == ["docker", "rm"] for call in actual["log"])


@pytest.mark.parametrize(
    "operation",
    [
        "restore",
        "backup-prepare",
        "backup-cleanup",
        "legacy-restore",
        "original-prepare",
        "commit-named",
        "commit-legacy",
        "cleanup",
    ],
)
@pytest.mark.parametrize("field,rc", [("{{.Id}}", 1), ("{{.Config.Image}}", 81), ("{{.Id}}", 0)])
def test_conditional_identity_queries_require_success_before_mutation(
    tmp_path, operation, field, rc
):
    import re

    source = DEPLOY.read_text()
    names = [
        "restore_named_identity",
        "prepare_named_full_backup_cleanup",
        "cleanup_named_full_backups",
        "rollback_legacy_moves",
        "prepare_original_cleanup",
        "prepare_commit_backups",
        "cleanup_originals",
        "require_retired_runtime",
    ]
    functions = "\n".join(
        re.search(rf"{name}\(\) \{{\n.*?\n\}}", source, re.S).group() for name in names
    )
    calls = {
        "restore": "restore_named_identity original relocated accepted-id accepted-image true",
        "backup-prepare": "prepare_named_full_backup_cleanup",
        "backup-cleanup": "cleanup_named_full_backups",
        "legacy-restore": "rollback_legacy_moves",
        "original-prepare": "prepare_original_cleanup",
        "commit-named": "prepare_commit_backups",
        "commit-legacy": "prepare_commit_backups",
        "cleanup": "cleanup_originals",
    }
    setup = r"""set -euo pipefail
runtime_inventory_recovery=none
processed=(); named_full_originals=(); named_full_backups=(); legacy_moves=(); legacy_previous_backups=(); commit_removals=()
names=relocated
case "$operation" in
  backup-*|commit-named) names=backup; named_full_backups=('original|backup|accepted-id|accepted-image|false') ;;
  legacy-restore) legacy_moves=('original|relocated|accepted-id|true|accepted-image') ;;
  original-prepare) names=previous; named_full_originals=('stable|previous|stable|accepted-id|accepted-image|false|-|-') ;;
  commit-legacy) names=backup; legacy_previous_backups=(backup); legacy_moves=('original|backup|accepted-id|false|accepted-image') ;;
  cleanup) commit_removals=('original|accepted-id|accepted-image|original') ;;
esac
docker() {
  if [ "$1" = container ]; then
    if [ "$2" = ls ]; then printf '%s\n' "$names"; else [ "$3" = "$names" ]; fi
  elif [ "$1" = inspect ]; then
    case "$3" in
      *'{{.Id}}'*) printf '%s\n' accepted-id ;;
      *Config.Image*) printf '%s\n' accepted-image ;;
      *Running*|*OOMKilled*) printf '%s\n' false ;;
      *ExitCode*) printf '%s\n' 0 ;;
      *Error*) printf '\n' ;;
      *) return 92 ;;
    esac
    [ "$3" != "$failed_field" ] || return "$query_rc"
  else printf '%s\n' "$*" >> "$events"; fi
}
"""
    script = (
        setup
        + _inventory_policy(source)
        + functions
        + "\nif "
        + calls[operation]
        + '; then [ "$query_rc" = 0 ]; else [ "$query_rc" != 0 ]; fi\n'
    )
    events = tmp_path / "events"
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        env={
            **os.environ,
            "operation": operation,
            "failed_field": field,
            "query_rc": str(rc),
            "events": str(events),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    if rc:
        assert not events.exists(), "Matching stdout with failed status cannot authorize mutation"
    elif operation in ["restore", "legacy-restore", "backup-cleanup", "cleanup"]:
        assert events.exists(), "Successful status control must execute real mutation branch"


@pytest.mark.parametrize(
    "field,rc",
    [
        (field, rc)
        for field in [
            "{{.Config.Image}}",
            "{{.Path}}",
            "{{json .Args}}",
            '{{ join .Config.Cmd " " }}',
            '{{ index .Config.Labels "com.findb.fetcher.accepted" }}',
        ]
        for rc in [1, 81]
    ]
    + [("{{.Config.Image}}", 0)],
)
def test_legacy_137_matching_identity_output_requires_query_success(field, rc):
    import re

    source = DEPLOY.read_text()
    functions = "\n".join(
        re.search(rf"{name}\(\) \{{\n.*?\n\}}", source, re.S).group()
        for name in ["legacy_historical_shutdown", "require_retired_runtime"]
    )
    setup = r"""set -euo pipefail
APP_ENVIRONMENT=staging; FETCHER_RUNTIME_PROFILE=bounded; AWS_ACCOUNT_ID=439622209937
expected_registry=439622209937.dkr.ecr.ap-southeast-1.amazonaws.com
docker() {
  case "$3" in
    *Running*|*OOMKilled*) printf '%s\n' false ;;
    *ExitCode*) printf '%s\n' 137 ;;
    *Error*|*historical-shutdown*) printf '\n' ;;
    *Config.Image*) printf '%s\n' "$expected_registry/findb/staging/fetcher/twelve-data@sha256:2f64ab8c40e082e6601a00839b2345c35c504afcf1d453026d779ac151f46d03" ;;
    *Path*) printf '%s\n' findb-fetch-historical-backfill ;;
    *Args*) printf '%s\n' '["--provider","twelve_data","--run-forever"]' ;;
    *Config.Cmd*) printf '%s\n' 'findb-fetch-historical-backfill --provider twelve_data --run-forever' ;;
    *accepted*) printf '%s\n' true ;;
    *) return 92 ;;
  esac
  [ "$3" != "$failed_field" ] || return "$query_rc"
}
"""
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            setup
            + functions
            + '\nif require_retired_runtime findb-fetcher-scheduler-historical; then [ "$query_rc" = 0 ]; else [ "$query_rc" != 0 ]; fi\n',
        ],
        env={**os.environ, "failed_field": field, "query_rc": str(rc)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert ("legacy_historical_crash_recovery" in result.stderr) == (rc == 0)
