#!/usr/bin/env bash
# findb_environment_contract=app-environment-v1
# Transactional Fetcher staging deployment. Secrets are loaded independently
# for each provider by the instance role and never cross this host boundary.

set -Eeuo pipefail
# A failed inspect is not proof of absence: Docker uses rc=1 for daemon and
# permission errors too. Only a successful exact-name inventory can prove absence.
runtime_inventory_recovery=none
runtime_inventory_failure() {
  echo "fetcher_runtime_inventory=failed reason=query_unknown" >&2
  case "$runtime_inventory_recovery" in
    transaction) abort_transaction 1 ;;
    helper) recover 1 ;;
    transaction_recovery) echo "fetcher_aws_deploy=failed reason=rollback_inventory_unverified" >&2; exit 1 ;;
    helper_recovery) echo "release_fetcher_provider=failed reason=recovery_failed" >&2; exit 1 ;;
    *) exit 1 ;;
  esac
}
runtime_exists() {
  local requested="$1" inventory name
  inventory="$(docker container ls --all --format '{{.Names}}' 2>/dev/null)" || runtime_inventory_failure
  while IFS= read -r name; do
    if [ "$name" = "$requested" ]; then
      docker container inspect "$requested" >/dev/null 2>&1 || runtime_inventory_failure
      return 0
    fi
  done <<< "$inventory"
  return 1
}

runtime_command_role() {
  local role
  role="$(python3 - "$1" <<'COMMAND'
import json
import sys
try:
    command = json.loads(sys.argv[1])
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
        raise ValueError("invalid command")
    roles = {
        "findb-fetch-full-market": "full_market",
        "findb-fetch-scheduler": "pilot",
        "findb-fetch-finlab-scheduler": "pilot",
        "findb-fetch-shioaji-scheduler": "pilot",
        "findb-fetch-taifex-pilot": "pilot",
        "findb-fetch-historical-backfill": "historical",
    }
    if command == ["python", "-m", "findb_fetcher"]:
        role = "readiness"
    elif command[0] == "findb-fetch-runtime":
        role = command[command.index("--execution-profile") + 1]
        if role not in {"pilot", "full_market"}:
            raise ValueError("invalid profile")
    else:
        role = roles[command[0]]
    print(role)
except Exception:
    raise SystemExit(1)
COMMAND
)" || {
    echo "fetcher_runtime_inventory=failed reason=command_unknown" >&2
    return 1
  }
  printf '%s\n' "$role"
}

set +x

: "${AWS_REGION:?AWS_REGION is required}"
: "${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID is required}"
: "${APP_ENVIRONMENT:?APP_ENVIRONMENT is required}"
if [ "${DEPLOYMENT_TARGET+x}" = x ] && [ "$DEPLOYMENT_TARGET" != "$APP_ENVIRONMENT" ]; then
  echo "application_environment=failed reason=legacy_environment_conflict" >&2
  exit 1
fi
: "${ECR_REGISTRY:?ECR_REGISTRY is required}"
: "${FETCHER_RELEASE_ROOT:?FETCHER_RELEASE_ROOT is required}"
: "${FETCHER_DEPLOY_MODE:?FETCHER_DEPLOY_MODE is required}"
: "${TWELVE_IMAGE_REF:?TWELVE_IMAGE_REF is required}"
: "${FINLAB_IMAGE_REF:?FINLAB_IMAGE_REF is required}"
: "${SHIOAJI_IMAGE_REF:?SHIOAJI_IMAGE_REF is required}"

FETCHER_RUNTIME_PROFILE=unified
export FETCHER_RUNTIME_PROFILE
FULL_MARKET_ENABLED="${FULL_MARKET_ENABLED:-false}"
export FULL_MARKET_ENABLED
PROVIDER_WORK_REQUIRED="${PROVIDER_WORK_REQUIRED:-false}"
export PROVIDER_WORK_REQUIRED
export FETCHER_ACCOUNT_STATE_PATH="/var/lib/findb-account/$APP_ENVIRONMENT/governor.sqlite3"
if [[ ! "$FETCHER_RUNTIME_PROFILE" =~ ^unified$ ]]; then
  echo "fetcher_aws_deploy=failed reason=runtime_profile_invalid" >&2
  exit 1
fi

expected_registry="$AWS_ACCOUNT_ID.dkr.ecr.ap-southeast-1.amazonaws.com"
if [ "$AWS_REGION" != ap-southeast-1 ] || ! [[ "$AWS_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || [[ ! "$APP_ENVIRONMENT" =~ ^(staging|production)$ ]] || [ "$ECR_REGISTRY" != "$expected_registry" ]; then
  echo "fetcher_aws_deploy=failed reason=aws_route_invalid" >&2
  exit 1
fi
case "$FETCHER_DEPLOY_MODE" in
  candidate|activate) ;;
  *) echo "fetcher_aws_deploy=failed reason=deploy_mode_invalid" >&2; exit 1 ;;
esac
  if ! printf '%s' "$FETCHER_RELEASE_ROOT" | grep -Eq '^/opt/fetcher/releases/[0-9a-f]{64}-[A-Za-z0-9._-]+$' \
  || [ ! -d "$FETCHER_RELEASE_ROOT" ] || [ -L "$FETCHER_RELEASE_ROOT" ] \
  || [ "$(stat -c '%U:%G:%a' "$FETCHER_RELEASE_ROOT")" != root:root:700 ]; then
  echo "fetcher_aws_deploy=failed reason=release_root_invalid" >&2
  exit 1
fi

runtime_dir="$FETCHER_RELEASE_ROOT/infra/deploy/runtime-secrets"
catalog="$runtime_dir/fetcher.json"
runtime_command="$runtime_dir/runtime_secret_command.sh"
provider_helper="$runtime_dir/release_fetcher_provider.sh"
for path in "$catalog" "$runtime_command" "$provider_helper"; do
  [ -f "$path" ] && [ ! -L "$path" ] || {
    echo "fetcher_aws_deploy=failed reason=release_artifact_missing" >&2
    exit 1
  }
done

manifest_profile="$(python3 - "$FETCHER_RELEASE_ROOT/release-manifest.json" <<'PROFILE'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    manifest = json.load(source)
print(manifest.get("runtime_profile"))
PROFILE
)"
if [ "$manifest_profile" != "$FETCHER_RUNTIME_PROFILE" ]; then
  echo "fetcher_aws_deploy=failed reason=runtime_profile_manifest_mismatch" >&2
  exit 1
fi

validate_image() {
  local image="$1" repository="$2"
  printf '%s' "$image" | grep -Eq "^${expected_registry}/${repository}@sha256:[0-9a-f]{64}$" || {
    echo "fetcher_aws_deploy=failed reason=ecr_image_contract" >&2
    exit 1
  }
}
validate_image "$TWELVE_IMAGE_REF" "findb/$APP_ENVIRONMENT/fetcher/twelve-data"
validate_image "$FINLAB_IMAGE_REF" "findb/$APP_ENVIRONMENT/fetcher/finlab"
validate_image "$SHIOAJI_IMAGE_REF" "findb/$APP_ENVIRONMENT/fetcher/shioaji"

# Only non-secret deployment configuration is preserved. The wrapper loads one
# consumer's allowlisted values into /run and removes them after the child exits.
preserve_env=PROVIDER_WORK_REQUIRED,FULL_MARKET_ENABLED,FETCHER_CONSUMER_PROFILE,FETCHER_ACCOUNT_STATE_PATH,FETCHER_RUNTIME_PROFILE,AWS_REGION,AWS_ACCOUNT_ID,APP_ENVIRONMENT,ECR_REGISTRY,FETCHER_RELEASE_ROOT,FETCHER_DEPLOY_MODE,FETCHER_PROVIDER_RELEASE_MODE,FETCHER_IMAGES_PREPARED,FETCHER_HEALTH_MODE,TWELVE_IMAGE_REF,FINLAB_IMAGE_REF,SHIOAJI_IMAGE_REF
export FETCHER_PROVIDER_RELEASE_MODE=transactional
run_runtime() {
  sudo --preserve-env="$preserve_env" "$runtime_command" \
    --catalog "$catalog" --region "$AWS_REGION" --deployment-target "$APP_ENVIRONMENT" --aws-account-id "$AWS_ACCOUNT_ID" "$@"
}

recorded_profile="$FETCHER_RUNTIME_PROFILE"
export FETCHER_RECORDED_RELEASE_PROFILE="$recorded_profile"
pilot_state=/var/lib/findb-pilot/$APP_ENVIRONMENT
full_market_state=/var/lib/findb-full-market/$APP_ENVIRONMENT
[ "$APP_ENVIRONMENT" != production ] || full_market_state=/var/lib/findb-full-market
require_full_checkpoint() {
  local full_market_state="${1:-$full_market_state}"
  # Installed workers own durable prepared work. Never replace a lost checkpoint,
  # including when an accepted replay invokes an older helper/image.
  if ! sudo test -f "$full_market_state/state.sqlite3" \
    || sudo test -L "$full_market_state/state.sqlite3" \
    || sudo test -L "$full_market_state"; then
    echo "fetcher_aws_deploy=failed reason=full_market_checkpoint_missing" >&2
    return 1
  fi
  if ! sudo python3 - "$full_market_state/state.sqlite3" <<'CHECKPOINT'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
try:
    with closing(sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("checkpoint integrity failed")
        core_columns = {'full_work': 'key,provider,plan_id,member_key,status,attempts,lease_until,body,body_sha256,receipt,reason,next_at', 'full_plan': 'plan_id,provider,dataset,trade_date,body,complete,late', 'full_quota': 'account,window,requests,bytes,blocked_until', 'full_cursor': 'provider,dataset,trade_date'}
        for table, columns in core_columns.items():
            db.execute(f"SELECT {columns} FROM {table} LIMIT 0")
except Exception:
    raise SystemExit(1)
CHECKPOINT
  then
    echo "fetcher_aws_deploy=failed reason=full_market_checkpoint_invalid" >&2
    return 1
  fi
}
initial_runtime_state() {
  local name="$1" id image running exit_code oom error accepted command
  id="$(docker inspect --format '{{.Id}}' "$name")" || runtime_inventory_failure
  image="$(docker inspect --format '{{.Config.Image}}' "$name")" || runtime_inventory_failure
  running="$(docker inspect --format '{{.State.Running}}' "$name")" || runtime_inventory_failure
  exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$name")" || runtime_inventory_failure
  oom="$(docker inspect --format '{{.State.OOMKilled}}' "$name")" || runtime_inventory_failure
  error="$(docker inspect --format '{{.State.Error}}' "$name")" || runtime_inventory_failure
  accepted="$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$name")" || runtime_inventory_failure
  command="$(docker inspect --format '{{json .Config.Cmd}}' "$name")" || runtime_inventory_failure
  runtime_command_role "$command" >/dev/null || runtime_inventory_failure
  [ -n "$id" ] && [ -n "$image" ] || runtime_inventory_failure
  case "$running:$oom" in true:true|true:false|false:true|false:false) ;; *) runtime_inventory_failure ;; esac
  [[ "$exit_code" =~ ^[0-9]+$ ]] || runtime_inventory_failure
  case "$accepted" in true|false|''|'<no value>') ;; *) runtime_inventory_failure ;; esac
}
initial_runtime_inventory="$(docker container ls --all --format '{{.Names}}' 2>/dev/null)" || runtime_inventory_failure
initial_runtime_exists() {
  local requested="$1" name
  while IFS= read -r name; do
    if [ "$name" = "$requested" ]; then
      docker container inspect "$requested" >/dev/null 2>&1 || runtime_inventory_failure
      return 0
    fi
  done <<< "$initial_runtime_inventory"
  return 1
}
retained_full_market=false
for pair in "findb-pilot-twelve-data:findb-full-market-twelve-data" "findb-pilot-finlab:findb-full-market-finlab" "findb-pilot-shioaji:findb-full-market-shioaji" "findb-pilot-taifex:findb-full-market-taifex"; do
  pilot_name="${pair%%:*}"
  full_name="${pair#*:}"
  for base in "$full_name" "$pilot_name" "findb-historical-${full_name#findb-full-market-}"; do
    for installed in "$base" "${base}-previous" "${base}-candidate" "${base}-transaction-backup" "${base}-legacy-previous" "${base}-preflight" "${base}-preflight-state" "${base}-preflight-control"; do
      if initial_runtime_exists "$installed"; then
        initial_runtime_state "$installed"
        installed_command="$(docker inspect --format '{{json .Config.Cmd}}' "$installed")" || runtime_inventory_failure
        installed_role="$(runtime_command_role "$installed_command")" || runtime_inventory_failure
        if [[ "$installed" == "$full_name"* ]] || [ "$installed_role" = full_market ]; then
          require_full_checkpoint
          retained_full_market=true
        elif [ "$installed_role" = pilot ]; then
          require_full_checkpoint "$pilot_state"
        fi
      fi
    done
  done
done

processed=()
named_full_originals=()
named_full_backups=()
rollback_failed=0
pointer_committed=0
previous_pointer=""
pointer_temp=""
register_named_full() {
  local stable="$1" previous="$2" name accepted id image running original=- original_id=- original_image=- original_running=false stable_id=- previous_id=- backup=- backup_id=- backup_image=- backup_running=false old_available=0
  # Capture every initial identity before publishing any recovery entry. Failed
  # inspection/collision must not enable generic destructive rollback.
  for name in "$stable" "$previous"; do
    if runtime_exists "$name"; then
      accepted="$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$name")" || return 1
      case "$accepted" in true|false|''|'<no value>') ;; *) return 1 ;; esac
      id="$(docker inspect --format '{{.Id}}' "$name")" || return 1
      image="$(docker inspect --format '{{.Config.Image}}' "$name")" || return 1
      running="$(docker inspect --format '{{.State.Running}}' "$name")" || return 1
      [ -n "$id" ] && [ -n "$image" ] || return 1
      case "$running" in true|false) ;; *) return 1 ;; esac
      if [ "$accepted" != false ]; then
        if [ "$name" = "$stable" ]; then stable_id="$id"; else previous_id="$id"; fi
        if [ "$original" = - ]; then
          original="$name"; original_id="$id"; original_image="$image"; original_running="$running"; old_available=1
        elif [ "$name" = "$previous" ]; then
          backup="${stable}-transaction-backup"; backup_id="$id"; backup_image="$image"; backup_running="$running"
          if runtime_exists "$backup"; then
            echo "fetcher_aws_deploy=failed reason=named_backup_collision runtime=$backup" >&2
            return 1
          fi
        fi
      elif [ "$name" = "$previous" ] && [ "$old_available" -eq 0 ]; then
        echo "fetcher_aws_deploy=failed reason=unaccepted_retained_runtime runtime=$previous" >&2
        return 1
      fi
    fi
  done
  # One complete original record includes BOTH initial IDs, so even a signal
  # between journal appends and staging cannot mistake a stale original for a
  # replacement. No runtime mutation precedes the processed registration.
  named_full_originals+=("${stable}|${previous}|${original}|${original_id}|${original_image}|${original_running}|${stable_id}|${previous_id}")
  if [ "$backup" != - ]; then named_full_backups+=("${previous}|${backup}|${backup_id}|${backup_image}|${backup_running}"); fi
  processed+=("${stable}:${previous}:${old_available}:${original_id}")
  if [ "$backup" != - ]; then docker rename "$previous" "$backup"; fi
}
restore_named_identity() {
  local original="$1" location="$2" original_id="$3" image="$4" was_running="$5" current actual_id actual_image
  if ! runtime_exists "$location"; then
    echo "fetcher_aws_deploy=failed reason=named_original_missing runtime=$original" >&2
    return 1
  fi
  actual_id="$(docker inspect --format '{{.Id}}' "$location")" || return 1
  actual_image="$(docker inspect --format '{{.Config.Image}}' "$location")" || return 1
  if [ "$actual_id" != "$original_id" ] || [ "$actual_image" != "$image" ]; then
    echo "fetcher_aws_deploy=failed reason=named_original_missing runtime=$original" >&2
    return 1
  fi
  if [ "$location" != "$original" ]; then docker rename "$location" "$original" >/dev/null || return 1; fi
  current="$(docker inspect --format '{{.State.Running}}' "$original")" || return 1
  case "$current" in true|false) ;; *) return 1 ;; esac
  if [ "$was_running" = true ] && [ "$current" != true ]; then
    docker start "$original" >/dev/null || return 1
  elif [ "$was_running" = false ] && [ "$current" = true ]; then
    docker stop --time 30 "$original" >/dev/null || return 1
    require_retired_runtime "$original" || return 1
  fi
}
rollback_named_full() {
  local stable="$1" previous="$2" original="$3" original_id="$4" image="$5" was_running="$6" stable_id="$7" previous_id="$8" location=- name running id actual_image
  for name in "$stable" "$previous"; do
    if runtime_exists "$name"; then
      id="$(docker inspect --format '{{.Id}}' "$name")" || return 1
      if [ "$id" = "$original_id" ]; then location="$name"; break; fi
    fi
  done
  if [ "$original" != - ]; then
    if [ "$location" = - ]; then
      echo "fetcher_aws_deploy=failed reason=named_original_missing runtime=$original" >&2
      return 1
    fi
    actual_image="$(docker inspect --format '{{.Config.Image}}' "$location")" || return 1
    if [ "$actual_image" != "$image" ]; then
      echo "fetcher_aws_deploy=failed reason=named_original_missing runtime=$original" >&2
      return 1
    fi
  fi
  for name in "$stable" "$previous"; do
    if [ "$name" != "$location" ] && runtime_exists "$name"; then
      id="$(docker inspect --format '{{.Id}}' "$name")" || return 1
      if [ "$id" = "$stable_id" ] || [ "$id" = "$previous_id" ]; then continue; fi
      running="$(docker inspect --format '{{.State.Running}}' "$name")" || return 1
      case "$running" in true|false) ;; *) return 1 ;; esac
      if [ "$running" = true ]; then docker stop --time 30 "$name" >/dev/null || return 1; fi
      require_retired_runtime "$name" || return 1
      docker rm "$name" >/dev/null || return 1
    fi
  done
  if [ "$original" != - ]; then restore_named_identity "$original" "$location" "$original_id" "$image" "$was_running"; fi
}
rollback_named_full_backups() {
  local index entry original backup original_id image was_running location
  for ((index=${#named_full_backups[@]}-1; index>=0; index--)); do
    entry="${named_full_backups[$index]}"
    IFS='|' read -r original backup original_id image was_running <<< "$entry"
    location="$original"
    if runtime_exists "$backup"; then location="$backup"; fi
    restore_named_identity "$original" "$location" "$original_id" "$image" "$was_running" || return 1
  done
}
prepare_named_full_backup_cleanup() {
  local entry original backup original_id image was_running running actual_id actual_image
  for entry in ${named_full_backups[@]+"${named_full_backups[@]}"}; do
    IFS='|' read -r original backup original_id image was_running <<< "$entry"
    runtime_exists "$backup" || return 1
    actual_id="$(docker inspect --format '{{.Id}}' "$backup")" || return 1
    actual_image="$(docker inspect --format '{{.Config.Image}}' "$backup")" || return 1
    [ "$actual_id" = "$original_id" ] && [ "$actual_image" = "$image" ] || return 1
    running="$(docker inspect --format '{{.State.Running}}' "$backup")" || return 1
    case "$running" in true|false) ;; *) return 1 ;; esac
    if [ "$running" = true ]; then docker stop --time 30 "$backup" >/dev/null || return 1; fi
    require_retired_runtime "$backup" || return 1
  done
}
cleanup_named_full_backups() {
  local entry original backup original_id image was_running actual_id actual_image
  for entry in ${named_full_backups[@]+"${named_full_backups[@]}"}; do
    IFS='|' read -r original backup original_id image was_running <<< "$entry"
    actual_id="$(docker inspect --format '{{.Id}}' "$backup")" || return 1
    actual_image="$(docker inspect --format '{{.Config.Image}}' "$backup")" || return 1
    [ "$actual_id" = "$original_id" ] && [ "$actual_image" = "$image" ] || return 1
    require_retired_runtime "$backup" || return 1
    docker rm "$backup" >/dev/null || return 1
  done
}

rollback_provider() {
  local stable="$1" previous="$2" old_available="$3" original_id="$4" state accepted entry named_stable named_previous original image was_running stable_id previous_id
  for entry in ${named_full_originals[@]+"${named_full_originals[@]}"}; do
    IFS='|' read -r named_stable named_previous original original_id image was_running stable_id previous_id <<< "$entry"
    if [ "$named_stable" = "$stable" ]; then
      rollback_named_full "$named_stable" "$named_previous" "$original" "$original_id" "$image" "$was_running" "$stable_id" "$previous_id"
      return $?
    fi
  done
  # Incomplete/unregistered snapshots never authorize generic cleanup.
  echo "fetcher_aws_deploy=failed reason=runtime_snapshot_missing runtime=$stable" >&2
  return 1
}
rollback_processed() {
  local index entry stable remainder previous old_available original_id
  for ((index=${#processed[@]}-1; index>=0; index--)); do
    entry="${processed[$index]}"
    stable="${entry%%:*}"
    remainder="${entry#*:}"
    previous="${remainder%%:*}"
    remainder="${remainder#*:}"
    old_available="${remainder%%:*}"
    original_id="${remainder#*:}"
    rollback_provider "$stable" "$previous" "$old_available" "$original_id" || rollback_failed=1
  done
  rollback_named_full_backups || rollback_failed=1
  if [ "$rollback_failed" -ne 0 ]; then
    echo "fetcher_aws_deploy=failed reason=transaction_rollback_failed" >&2
    return 1
  fi
}
abort_transaction() {
  local status="${1:-$?}"
  trap - ERR INT TERM HUP
  runtime_inventory_recovery=transaction_recovery
  rollback_processed || status=1
  if [ "$pointer_committed" -eq 1 ]; then
    pointer_temp="/opt/fetcher/.current-restore.$$.tmp"
    rm -f -- "$pointer_temp"
    if [ -n "$previous_pointer" ]; then
      ln -s -- "$previous_pointer" "$pointer_temp" \
        && mv -Tf -- "$pointer_temp" /opt/fetcher/current || status=1
    else
      [ -L /opt/fetcher/current ] && rm -- /opt/fetcher/current || status=1
    fi
  fi
  [ -z "$pointer_temp" ] || rm -f -- "$pointer_temp"
  echo "fetcher_aws_deploy=failed reason=transaction_aborted" >&2
  exit "$status"
}
trap 'abort_transaction "$?"' ERR
trap 'abort_transaction 130' INT
trap 'abort_transaction 143' TERM
trap 'abort_transaction 129' HUP
runtime_inventory_recovery=transaction

register_provider() {
  # The complete record policy applies equally to Pilot, Full and historical.
  register_named_full "$1" "$2"
}

if [ "$FETCHER_DEPLOY_MODE" = activate ]; then
  if [ -e /opt/fetcher/current ] && [ ! -L /opt/fetcher/current ]; then
    echo "fetcher_aws_deploy=failed reason=current_pointer_invalid" >&2
    false
  fi
  if [ -L /opt/fetcher/current ]; then
    previous_pointer="$(readlink /opt/fetcher/current)"
    printf '%s' "$previous_pointer" | grep -Eq '^/opt/fetcher/releases/[0-9a-f]{64}-[A-Za-z0-9._-]+$' || {
      echo "fetcher_aws_deploy=failed reason=current_pointer_invalid" >&2
      false
    }
  fi
  pointer_temp="/opt/fetcher/.current-activate.$$.tmp"
  rm -f -- "$pointer_temp"
  ln -s -- "$FETCHER_RELEASE_ROOT" "$pointer_temp"
  pointer_committed=1
  mv -Tf -- "$pointer_temp" /opt/fetcher/current
  pointer_temp=""
fi


require_retired_runtime() {
  local stable="$1" legacy_identity="${2:-$1}" running exit_code oom error
  running="$(docker inspect --format '{{.State.Running}}' "$stable")" || return 1
  case "$running" in true|false) ;; *) return 1 ;; esac
  exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$stable")" || return 1
  oom="$(docker inspect --format '{{.State.OOMKilled}}' "$stable")" || return 1
  error="$(docker inspect --format '{{.State.Error}}' "$stable")" || return 1
  if [ "$running" = false ] && [ "$oom" = false ] && [ -z "$error" ]; then
    if [ "$exit_code" = 0 ]; then
      return 0
    fi
  fi
  # Docker's Error may include host details. Emit only a fixed safe reason.
  echo "fetcher_aws_deploy=failed reason=runtime_retirement_failed runtime=$stable" >&2
  return 1
}

registered_original() {
  local requested="$1" entry stable previous original id image running stable_id previous_id
  for entry in ${named_full_originals[@]+"${named_full_originals[@]}"}; do
    IFS='|' read -r stable previous original id image running stable_id previous_id <<< "$entry"
    if [ "$stable" = "$requested" ]; then
      printf '%s|%s|%s\n' "$original" "$id" "$image"
      return 0
    fi
  done
  return 1
}

# A first cutover must drain and archive legacy pilot containers before this
# immutable release is installed. Never restart pre-unification workers here.
# Pull each exact image once per phase with a secret-free consumer. Individual
# provider credentials remain confined to their own runtime wrapper invocation.
run_runtime --consumer images --ecr-registry "$ECR_REGISTRY" --docker-login -- \
  bash -s -- "$TWELVE_IMAGE_REF" "$FINLAB_IMAGE_REF" "$SHIOAJI_IMAGE_REF" <<'PREPARE_IMAGES'
set -euo pipefail
for image in "$@"; do
  docker pull "$image" >/dev/null
  docker image inspect "$image" --format '{{range .RepoDigests}}{{println .}}{{end}}' | grep -Fxq "$image"
done
PREPARE_IMAGES
export FETCHER_IMAGES_PREPARED=true FETCHER_HEALTH_MODE=coordinator
activated_runtimes=()
record_activated_runtime() {
  local stable="$1" image="$2" id
  [ "$FETCHER_DEPLOY_MODE" = activate ] || return 0
  id="$(docker inspect --format '{{.Id}}' "$stable")" || return 1
  [ -n "$id" ] || return 1
  activated_runtimes+=("$stable|$id|$image")
}
verify_activated_runtimes() {
  local attempt entry stable id image actual_id actual_image running restarts
  # Six checks spanning at least 25 seconds for EVERY new consumer. Mutations
  # stay serial; only observation windows overlap. Originals remain journaled.
  for attempt in 1 2 3 4 5 6; do
    for entry in "${activated_runtimes[@]}"; do
      IFS='|' read -r stable id image <<< "$entry"
      actual_id="$(docker inspect --format '{{.Id}}' "$stable")" || return 1
      actual_image="$(docker inspect --format '{{.Config.Image}}' "$stable")" || return 1
      running="$(docker inspect --format '{{.State.Running}}' "$stable")" || return 1
      restarts="$(docker inspect --format '{{.RestartCount}}' "$stable")" || return 1
      if [ "$actual_id" != "$id" ] || [ "$actual_image" != "$image" ] \
        || [ "$running" != true ] || [ "$restarts" != 0 ]; then
        echo "fetcher_aws_deploy=failed reason=activation_unhealthy runtime=$stable" >&2
        return 1
      fi
    done
    [ "$attempt" -eq 6 ] || sleep 5
  done
}

# Never restart pre-unification pilot workers during this release.
for legacy in findb-fetcher-scheduler findb-fetcher-finlab-scheduler findb-fetcher-shioaji-scheduler findb-fetcher-taifex-scheduler; do
  if runtime_exists "$legacy"; then
    echo "fetcher_aws_deploy=failed reason=legacy_runtime_requires_cutover" >&2
    exit 1
  fi
done
provider_count=8
for execution_profile in pilot full_market; do
  export FETCHER_CONSUMER_PROFILE="$execution_profile"
  state_dir="$pilot_state"
  name_prefix=findb-pilot
  if [ "$execution_profile" = full_market ]; then
    state_dir="$full_market_state"
    name_prefix=findb-full-market
  fi
  for provider in twelve-data finlab shioaji taifex; do
    identity="${provider//-/_}"
    case "$provider" in
      twelve-data|taifex) image="$TWELVE_IMAGE_REF"; cache_dir=- ;;
      finlab) image="$FINLAB_IMAGE_REF"; cache_dir=/var/lib/findb-finlab-fetcher/cache ;;
      shioaji) image="$SHIOAJI_IMAGE_REF"; cache_dir=/var/lib/findb-shioaji-fetcher/cache ;;
    esac
    stable="$name_prefix-$provider"
    register_provider "$stable" "${stable}-previous"
    run_runtime --consumer "$provider" -- \
      "$provider_helper" "$provider" "$image" \
      "$state_dir" "$state_dir/state.sqlite3" \
      "$stable" "${stable}-candidate" "${stable}-previous" "${stable}-preflight" "$cache_dir" \
      findb-fetch-runtime --execution-profile "$execution_profile" \
      --config "/app/configs/full_market.$APP_ENVIRONMENT.v1.json" \
      --state-path "$state_dir/state.sqlite3" --provider "$identity"
    record_activated_runtime "$stable" "$image"
  done
done

# Historical requests retain their existing provider-specific durable directory.
# They are independently journaled consumers, never side effects of pilot release.
export FETCHER_CONSUMER_PROFILE=historical
# historical-taifex-v1: accepted installations require all four historical consumers.
for provider in twelve-data finlab shioaji taifex; do
  case "$provider" in
    twelve-data) image="$TWELVE_IMAGE_REF"; state_dir=/var/lib/findb-fetcher/historical; cache_dir=- ;;
    finlab) image="$FINLAB_IMAGE_REF"; state_dir=/var/lib/findb-finlab-fetcher/historical; cache_dir=/var/lib/findb-finlab-fetcher/cache ;;
    shioaji) image="$SHIOAJI_IMAGE_REF"; state_dir=/var/lib/findb-shioaji-fetcher/historical; cache_dir=/var/lib/findb-shioaji-fetcher/cache ;;
    taifex) image="$TWELVE_IMAGE_REF"; state_dir=/var/lib/findb-taifex-fetcher/historical; cache_dir=- ;;
  esac
  stable="findb-historical-$provider"
  register_provider "$stable" "${stable}-previous"
  run_runtime --consumer "$provider" -- \
    "$provider_helper" "$provider" "$image" "$state_dir" "$state_dir/state.sqlite3" \
    "$stable" "${stable}-candidate" "${stable}-previous" "${stable}-preflight" "$cache_dir" \
    findb-fetch-historical-backfill --provider "${provider//-/_}"
  record_activated_runtime "$stable" "$image"
done

if [ "$FETCHER_DEPLOY_MODE" = candidate ]; then
  rollback_processed
  trap - ERR INT TERM HUP
  echo "fetcher_aws_deploy=candidate_ready_for_acceptance providers=$provider_count profile=$FETCHER_RUNTIME_PROFILE"
  exit 0
fi

verify_activated_runtimes

commit_removals=()
prepare_original_cleanup() {
  local entry stable previous original original_id image was_running stable_id previous_id location name id actual_image
  for entry in ${named_full_originals[@]+"${named_full_originals[@]}"}; do
    IFS='|' read -r stable previous original original_id image was_running stable_id previous_id <<< "$entry"
    [ "$original" != - ] || continue
    location=-
    for name in "$stable" "$previous"; do
      if runtime_exists "$name"; then
        id="$(docker inspect --format '{{.Id}}' "$name")" || return 1
        if [ "$id" = "$original_id" ]; then location="$name"; break; fi
      fi
    done
    [ "$location" != - ] || return 1
    actual_image="$(docker inspect --format '{{.Config.Image}}' "$location")" || return 1
    [ "$actual_image" = "$image" ] || return 1
    # Only a replaced original is eligible. An unchanged original is retained.
    [ "$location" != "$stable" ] || continue
    require_retired_runtime "$location" "${original%-previous}" || return 1
    commit_removals+=("${location}|${original_id}|${image}|${original%-previous}")
  done
}
prepare_commit_backups() {
  local entry original backup id image was_running running legacy_entry legacy_original full legacy_running original_id original_image actual_id actual_image
  for entry in ${named_full_backups[@]+"${named_full_backups[@]}"}; do
    IFS='|' read -r original backup id image was_running <<< "$entry"
    actual_id="$(docker inspect --format '{{.Id}}' "$backup")" || return 1
    actual_image="$(docker inspect --format '{{.Config.Image}}' "$backup")" || return 1
    [ "$actual_id" = "$id" ] && [ "$actual_image" = "$image" ] || return 1
    running="$(docker inspect --format '{{.State.Running}}' "$backup")" || return 1
    case "$running" in true|false) ;; *) return 1 ;; esac
    if [ "$running" = true ]; then docker stop --time 30 "$backup" >/dev/null || return 1; fi
    require_retired_runtime "$backup" "${original%-previous}" || return 1
    commit_removals+=("${backup}|${id}|${image}|${original%-previous}")
  done

}
cleanup_originals() {
  local entry name id image legacy_identity actual_id actual_image
  for entry in ${commit_removals[@]+"${commit_removals[@]}"}; do
    IFS='|' read -r name id image legacy_identity <<< "$entry"
    # Revalidate after commit: Docker state may change after preparation. Queries
    # and removal address the captured ID, never a name-reused replacement.
    actual_id="$(docker inspect --format '{{.Id}}' "$id")" || return 1
    actual_image="$(docker inspect --format '{{.Config.Image}}' "$id")" || return 1
    [ "$actual_id" = "$id" ] && [ "$actual_image" = "$image" ] || return 1
    require_retired_runtime "$id" "$legacy_identity" || return 1
    actual_id="$(docker inspect --format '{{.Id}}' "$id")" || return 1
    actual_image="$(docker inspect --format '{{.Config.Image}}' "$id")" || return 1
    [ "$actual_id" = "$id" ] && [ "$actual_image" = "$image" ] || return 1
    docker rm "$id" >/dev/null || return 1
  done
}
prepare_original_cleanup
prepare_commit_backups
# All fallible identity/state preparation precedes successful commit. Deletion
# afterward stays non-force and refuses any unexpected identity/state change.
trap - ERR INT TERM HUP
if ! cleanup_originals; then
  echo "fetcher_aws_deploy=failed reason=committed_cleanup_failed committed=true" >&2
  exit 1
fi
# Only after all consumers are healthy and rollback originals are retired.
# Never prune between consumers/phases and evict the next prepared image.
if ! docker image prune -af >/dev/null; then
  echo "fetcher_aws_deploy=warning reason=post_activation_image_prune_failed" >&2
fi
echo "fetcher_aws_deploy=activated providers=$provider_count profile=$FETCHER_RUNTIME_PROFILE"
