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

FETCHER_RUNTIME_PROFILE="${FETCHER_RUNTIME_PROFILE:-bounded}"
export FETCHER_RUNTIME_PROFILE
FULL_MARKET_ENABLED="${FULL_MARKET_ENABLED:-false}"
export FULL_MARKET_ENABLED
export FETCHER_ACCOUNT_STATE_PATH="/var/lib/findb-account/$APP_ENVIRONMENT/governor.sqlite3"
if [[ ! "$FETCHER_RUNTIME_PROFILE" =~ ^(bounded|full-market)$ ]]; then
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
print(manifest.get("runtime_profile", "bounded"))
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
preserve_env=FETCHER_ACCEPTED_REPLAY,FETCHER_LEGACY_ENV_BRIDGE,FULL_MARKET_ENABLED,FETCHER_CONSUMER_PROFILE,FETCHER_ACCOUNT_STATE_PATH,FETCHER_RUNTIME_PROFILE,FETCHER_RECORDED_RELEASE_PROFILE,AWS_REGION,AWS_ACCOUNT_ID,APP_ENVIRONMENT,ECR_REGISTRY,FETCHER_RELEASE_ROOT,FETCHER_DEPLOY_MODE,FETCHER_PROVIDER_RELEASE_MODE,FETCHER_SOURCE_API_URL,FINDB_SERVE_BASE_URL,FETCHER_CALENDAR_TIMEOUT_SECONDS,FETCHER_CALENDAR_CACHE_TTL_SECONDS,CLOUDFLARE_R2_ACCOUNT_ID,CLOUDFLARE_R2_RAW_BUCKET,CLOUDFLARE_R2_MAX_OBJECT_BYTES,FETCHER_REQUEST_TIMEOUT_SECONDS,FETCHER_SCHEDULER_CONTROL_POLL_SECONDS,FETCHER_MAX_ATTEMPTS,FETCHER_MAX_RETRY_AFTER_SECONDS,TWELVE_DATA_BASE_URL,TWELVE_DATA_TIMEOUT_SECONDS,TWELVE_DATA_MAX_RESPONSE_BYTES,TAIFEX_BASE_URL,TAIFEX_TIMEOUT_SECONDS,TAIFEX_MAX_RESPONSE_BYTES,SHIOAJI_SIMULATION,TWELVE_IMAGE_REF,FINLAB_IMAGE_REF,SHIOAJI_IMAGE_REF
export FETCHER_PROVIDER_RELEASE_MODE=transactional
schedule_file=/app/configs/daily_scheduler.staging.v3.json
shioaji_manifest=/app/configs/shioaji_tw_staging_pilot.v3.json
twelve_state=/var/lib/findb-fetcher/staging-pilot-v2
shioaji_state=/var/lib/findb-shioaji-fetcher/staging-pilot-v3
if [ "$APP_ENVIRONMENT" = production ]; then
  twelve_state=/var/lib/findb-fetcher
  shioaji_state=/var/lib/findb-shioaji-fetcher
  schedule_file=/app/configs/daily_scheduler.production.v3.json
  shioaji_manifest=/app/configs/shioaji_tw50_2026_09_21.v2.json
fi

run_runtime() {
  sudo --preserve-env="$preserve_env" "$runtime_command" \
    --catalog "$catalog" --region "$AWS_REGION" --deployment-target "$APP_ENVIRONMENT" --aws-account-id "$AWS_ACCOUNT_ID" "$@"
}

recorded_profile="$FETCHER_RUNTIME_PROFILE"
export FETCHER_RECORDED_RELEASE_PROFILE="$recorded_profile"
full_market_state=/var/lib/findb-full-market/$APP_ENVIRONMENT
[ "$APP_ENVIRONMENT" != production ] || full_market_state=/var/lib/findb-full-market
require_full_checkpoint() {
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
for pair in "findb-fetcher-scheduler:findb-full-market-twelve-data" "findb-fetcher-finlab-scheduler:findb-full-market-finlab" "findb-fetcher-shioaji-scheduler:findb-full-market-shioaji" "findb-fetcher-taifex-scheduler:findb-full-market-taifex"; do
  pilot_name="${pair%%:*}"
  full_name="${pair#*:}"
  for base in "$full_name" "$pilot_name" "${full_name}-historical" "${pilot_name}-historical"; do
    for installed in "$base" "${base}-previous" "${base}-candidate" "${base}-transaction-backup" "${base}-legacy-previous" "${base}-preflight" "${base}-preflight-state" "${base}-preflight-control"; do
      if initial_runtime_exists "$installed"; then
        initial_runtime_state "$installed"
        installed_command="$(docker inspect --format '{{json .Config.Cmd}}' "$installed")" || runtime_inventory_failure
        installed_role="$(runtime_command_role "$installed_command")" || runtime_inventory_failure
        if [[ "$installed" == "$full_name"* ]] || [ "$installed_role" = full_market ]; then
          require_full_checkpoint
          retained_full_market=true
        fi
      fi
    done
  done
done

processed=()
legacy_moves=()
legacy_previous_backups=()
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
  rollback_legacy_moves || rollback_failed=1
  if [ "$rollback_failed" -ne 0 ]; then
    echo "fetcher_aws_deploy=failed reason=transaction_rollback_failed" >&2
    return 1
  fi
}
rollback_legacy_moves() {
  local index entry original full original_id was_running image current location actual_id actual_image
  for ((index=${#legacy_moves[@]}-1; index>=0; index--)); do
    entry="${legacy_moves[$index]}"
    IFS='|' read -r original full original_id was_running image <<< "$entry"
    location="$original"
    if ! runtime_exists "$original"; then
      location="$full"
    fi
    if ! runtime_exists "$location"; then
      echo "fetcher_aws_deploy=failed reason=legacy_original_missing runtime=$original" >&2
      return 1
    fi
    actual_id="$(docker inspect --format '{{.Id}}' "$location")" || return 1
    actual_image="$(docker inspect --format '{{.Config.Image}}' "$location")" || return 1
    if [ "$actual_id" != "$original_id" ] || [ "$actual_image" != "$image" ]; then
      echo "fetcher_aws_deploy=failed reason=legacy_original_missing runtime=$original" >&2
      return 1
    fi
    if [ "$location" != "$original" ]; then
      docker rename "$location" "$original" >/dev/null || return 1
    fi
    current="$(docker inspect --format '{{.State.Running}}' "$original")" || return 1
    case "$current" in true|false) ;; *) return 1 ;; esac
    if [ "$was_running" = true ] && [ "$current" != true ]; then
      docker start "$original" >/dev/null || return 1
    elif [ "$was_running" = false ] && [ "$current" = true ]; then
      docker stop --time 30 "$original" >/dev/null || return 1
      require_retired_runtime "$original" || return 1
    fi
  done
}
cleanup_legacy_previous_backups() {
  local backup
  for backup in ${legacy_previous_backups[@]+"${legacy_previous_backups[@]}"}; do
    require_retired_runtime "$backup" || return 1
    docker rm "$backup" >/dev/null || return 1
  done
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

legacy_historical_shutdown() {
  local stable="$1" identity="${2:-$1}" provider repository digest shutdown_marker image path args command accepted
  [ "$APP_ENVIRONMENT" = staging ] && [ "$FETCHER_RUNTIME_PROFILE" = bounded ] || return 1
  case "$identity" in
    findb-fetcher-scheduler-historical)
      provider=twelve_data; repository=twelve-data; digest=2f64ab8c40e082e6601a00839b2345c35c504afcf1d453026d779ac151f46d03 ;;
    findb-fetcher-finlab-scheduler-historical)
      provider=finlab; repository=finlab; digest=bcd882dff3412a55f14921474d16ca27950dc51f37252fae404b03eccb862bc2 ;;
    findb-fetcher-shioaji-scheduler-historical)
      provider=shioaji; repository=shioaji; digest=161991e5b056becbdc35d67c547a7092807f1e0aa66a34b7b921ed57ecc6bd13 ;;
    *) return 1 ;;
  esac
  shutdown_marker="$(docker inspect --format '{{range $key, $_ := .Config.Labels}}{{if eq $key "com.findb.fetcher.historical-shutdown"}}present{{end}}{{end}}' "$stable")" || return 1
  # Transitional exception for the observed pre-signal-handler staging binaries only.
  # They ignore SIGTERM as PID 1. Leased dates replay after server expiry;
  # this is crash recovery, never a successful graceful shutdown.
  image="$(docker inspect --format '{{.Config.Image}}' "$stable")" || return 1
  path="$(docker inspect --format '{{.Path}}' "$stable")" || return 1
  args="$(docker inspect --format '{{json .Args}}' "$stable")" || return 1
  command="$(docker inspect --format '{{ join .Config.Cmd " " }}' "$stable")" || return 1
  accepted="$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$stable")" || return 1
  [ "$AWS_ACCOUNT_ID" = 439622209937 ] \
    && [ "$image" = "$expected_registry/findb/staging/fetcher/$repository@sha256:$digest" ] \
    && [ "$path" = findb-fetch-historical-backfill ] \
    && [ "$args" = "[\"--provider\",\"$provider\",\"--run-forever\"]" ] \
    && [ "$command" = "findb-fetch-historical-backfill --provider $provider --run-forever" ] \
    && [ "$accepted" = true ] \
    && [ -z "$shutdown_marker" ]
}

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
    if [ "$exit_code" = 137 ] && legacy_historical_shutdown "$stable" "$legacy_identity"; then
      echo "fetcher_aws_deploy=legacy_historical_crash_recovery runtime=$stable lease_policy=expire_and_replay" >&2
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
retire_runtime() {
  local stable="$1" previous="${1}-previous" running original=- original_id=- original_image=- snapshot name id accepted actual_image
  register_provider "$stable" "$previous"
  snapshot="$(registered_original "$stable")" || return 1
  IFS='|' read -r original original_id original_image <<< "$snapshot"
  # A failed candidate is separate from the selected accepted original.
  if runtime_exists "$stable"; then
    accepted="$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$stable")" || return 1
    if [ "$accepted" = false ]; then
      running="$(docker inspect --format '{{.State.Running}}' "$stable")" || return 1
      case "$running" in true|false) ;; *) return 1 ;; esac
      if [ "$running" = true ]; then docker stop --time 30 "$stable" >/dev/null || return 1; fi
      require_retired_runtime "$stable" || return 1
      docker rm "$stable" >/dev/null || return 1
    fi
  fi
  [ "$original" != - ] || return 0
  name="$original"
  id="$(docker inspect --format '{{.Id}}' "$name")" || return 1
  actual_image="$(docker inspect --format '{{.Config.Image}}' "$name")" || return 1
  [ "$id" = "$original_id" ] && [ "$actual_image" = "$original_image" ] || return 1
  running="$(docker inspect --format '{{.State.Running}}' "$name")" || return 1
  case "$running" in true|false) ;; *) return 1 ;; esac
  if [ "$running" = true ]; then
    if ! docker stop --time 30 "$name" >/dev/null 2>&1; then
      echo "fetcher_aws_deploy=failed reason=runtime_stop_failed runtime=$name" >&2
      return 1
    fi
  fi
  require_retired_runtime "$name" "$stable" || return 1
  if [ "$name" = "$stable" ]; then
    if runtime_exists "$previous"; then
      echo "fetcher_aws_deploy=failed reason=unstaged_previous runtime=$previous" >&2
      return 1
    fi
    docker rename "$stable" "$previous" || return 1
  fi
}

move_legacy_full_runtime() {
  local original="$1" full="$2" role="${3:-current}" original_id was_running image backup accepted
  accepted="$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$original")" || return 1
  case "$accepted" in true|''|'<no value>') ;; *) return 1 ;; esac
  if [ "$role" = current ] && runtime_exists "${original}-previous"; then
    backup="${full}-legacy-previous"
    if runtime_exists "$backup"; then
      echo "fetcher_aws_deploy=failed reason=legacy_backup_collision runtime=$backup" >&2
      return 1
    fi
    # Keep an older accepted previous out of Pilot's recovery selection. Its
    # own journal entry restores it only after the current original is restored.
    move_legacy_full_runtime "${original}-previous" "$backup" previous || return 1
    legacy_previous_backups+=("$backup")
  fi
  original_id="$(docker inspect --format '{{.Id}}' "$original")" || return 1
  was_running="$(docker inspect --format '{{.State.Running}}' "$original")" || return 1
  image="$(docker inspect --format '{{.Config.Image}}' "$original")" || return 1
  [ -n "$original_id" ] && [ -n "$image" ] || return 1
  case "$was_running" in true|false) ;; *) return 1 ;; esac
  # Record one complete recovery entry before stop/rename can receive a signal.
  legacy_moves+=("${original}|${full}|${original_id}|${was_running}|${image}")
  if [ "$was_running" = true ]; then
    docker stop --time 30 "$original" >/dev/null || return 1
  fi
  require_retired_runtime "$original" || return 1
  docker rename "$original" "$full"
}

# Preserve historical workers for transaction recovery. Only bounded creates
# replacement workers; expanded runtime never consumes historical requests.
for stable in findb-fetcher-scheduler findb-fetcher-finlab-scheduler findb-fetcher-shioaji-scheduler; do
  retire_runtime "${stable}-historical"
done
provider_count=3
for pair in "findb-fetcher-scheduler:findb-full-market-twelve-data" "findb-fetcher-finlab-scheduler:findb-full-market-finlab" "findb-fetcher-shioaji-scheduler:findb-full-market-shioaji" "findb-fetcher-taifex-scheduler:findb-full-market-taifex"; do
  pilot_name="${pair%%:*}"
  full_name="${pair#*:}"
  if runtime_exists "$full_name"; then
    require_full_checkpoint
    retained_full_market=true
  elif runtime_exists "${full_name}-previous"; then
    require_full_checkpoint
    retained_full_market=true
  else
    # Select the accepted original before interpreting its command. An
    # interrupted stable is never the migration original or a stale backup.
    legacy_original=-
    for installed in "$pilot_name" "${pilot_name}-previous"; do
      if runtime_exists "$installed"; then
        accepted="$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$installed")" || false
        case "$accepted" in
          true|''|'<no value>') legacy_original="$installed"; break ;;
          false) ;;
          *) false ;;
        esac
      fi
    done
    if [ "$legacy_original" != - ]; then
      installed_command="$(docker inspect --format '{{json .Config.Cmd}}' "$legacy_original")" || false
      installed_role="$(runtime_command_role "$installed_command")" || false
      if [ "$installed_role" = full_market ]; then
        require_full_checkpoint
        if [ "$legacy_original" = "$pilot_name" ]; then
          move_legacy_full_runtime "$legacy_original" "$full_name"
        else
          move_legacy_full_runtime "$legacy_original" "${full_name}-previous" previous
        fi
        retained_full_market=true
      fi
    fi
  fi
done
if [ "$recorded_profile" = full-market ] || [ "$retained_full_market" = true ]; then
  # A bounded flag-off release upgrades existing workers to immediate drain-only
  # authorization, preserving prepared bodies in the original checkpoint path.
  export FETCHER_RUNTIME_PROFILE=full-market
  export FETCHER_CONSUMER_PROFILE=full_market
  for provider in twelve-data finlab shioaji taifex; do
    case "$provider" in
      twelve-data) identity=twelve_data; image="$TWELVE_IMAGE_REF"; stable=findb-full-market-twelve-data ;;
      finlab) identity=finlab; image="$FINLAB_IMAGE_REF"; stable=findb-full-market-finlab ;;
      shioaji) identity=shioaji; image="$SHIOAJI_IMAGE_REF"; stable=findb-full-market-shioaji ;;
      taifex) identity=taifex; image="$TWELVE_IMAGE_REF"; stable=findb-full-market-taifex ;;
    esac
    if [ "$recorded_profile" = bounded ] && ! runtime_exists "$stable" && ! runtime_exists "${stable}-previous"; then
      continue
    fi
    register_provider "$stable" "${stable}-previous"
    export FETCHER_LEGACY_ENV_BRIDGE=false
    if [ "$recorded_profile" = bounded ] && [ "${FETCHER_ACCEPTED_REPLAY:-false}" = true ]; then
      # A bounded rollback may predate drain support. Preserve the compatible
      # installed image while recreating its flag-off environment/checkpoint.
      snapshot="$(registered_original "$stable")"
      IFS='|' read -r image_container original_id image <<< "$snapshot"
      [ "$image_container" != - ] && [ "$original_id" != - ] && [ "$image" != - ] || false
      export FETCHER_LEGACY_ENV_BRIDGE=true
    fi
    cache_dir=-
    case "$provider" in
      finlab) cache_dir=/var/lib/findb-finlab-fetcher/cache ;;
      shioaji) cache_dir=/var/lib/findb-shioaji-fetcher/cache ;;
    esac
    run_runtime --consumer "$provider" --ecr-registry "$ECR_REGISTRY" --docker-login -- \
      "$provider_helper" "$provider" "$image" \
      "$full_market_state" "$full_market_state/state.sqlite3" \
      "$stable" "${stable}-candidate" "${stable}-previous" "${stable}-preflight" "$cache_dir" \
      findb-fetch-full-market --config "/app/configs/full_market.$APP_ENVIRONMENT.v1.json" \
      --state-path "$full_market_state/state.sqlite3" \
      --provider "$identity" --readiness-file "/var/lib/findb-account/readiness/$identity.json"
  done
  provider_count=7
fi
# Pilot runtimes coexist with full-market workers; checkpoint identities stay stable.
export FETCHER_RUNTIME_PROFILE="$recorded_profile"
export FETCHER_CONSUMER_PROFILE=pilot
unset FETCHER_LEGACY_ENV_BRIDGE
if true; then
  if [ "$APP_ENVIRONMENT" = staging ]; then
    register_provider findb-fetcher-taifex-scheduler findb-fetcher-taifex-scheduler-previous
    taifex_state=/var/lib/findb-taifex-fetcher/staging-pilot-v1
    run_runtime --consumer taifex --ecr-registry "$ECR_REGISTRY" --docker-login -- \
      "$provider_helper" taifex "$TWELVE_IMAGE_REF" \
      "$taifex_state" "$taifex_state/state.sqlite3" \
      findb-fetcher-taifex-scheduler findb-fetcher-taifex-scheduler-candidate findb-fetcher-taifex-scheduler-previous \
      findb-fetcher-taifex-scheduler-preflight - \
      findb-fetch-taifex-pilot --config /app/configs/taifex_tw_staging_pilot.v1.json
    provider_count=4
  else
    retire_runtime findb-fetcher-taifex-scheduler
  fi
  register_provider findb-fetcher-scheduler findb-fetcher-scheduler-previous
  run_runtime --consumer twelve-data --ecr-registry "$ECR_REGISTRY" --docker-login -- \
    "$provider_helper" twelve-data "$TWELVE_IMAGE_REF" \
    "$twelve_state" "$twelve_state/state.sqlite3" \
    findb-fetcher-scheduler findb-fetcher-scheduler-candidate findb-fetcher-scheduler-previous \
    findb-fetcher-scheduler-preflight - \
    findb-fetch-scheduler --schedule-file "$schedule_file" \
    --slot-id western_markets_window --dataset-key us_equity_eod

  register_provider findb-fetcher-finlab-scheduler findb-fetcher-finlab-scheduler-previous
  run_runtime --consumer finlab --ecr-registry "$ECR_REGISTRY" --docker-login -- \
    "$provider_helper" finlab "$FINLAB_IMAGE_REF" \
    /var/lib/findb-finlab-fetcher /var/lib/findb-finlab-fetcher/state.sqlite3 \
    findb-fetcher-finlab-scheduler findb-fetcher-finlab-scheduler-candidate findb-fetcher-finlab-scheduler-previous \
    findb-fetcher-finlab-scheduler-preflight /var/lib/findb-finlab-fetcher/cache \
    findb-fetch-finlab-scheduler --schedule-file "$schedule_file" \
    --slot-id taiwan_market_window --dataset-key tw_equity_eod

  register_provider findb-fetcher-shioaji-scheduler findb-fetcher-shioaji-scheduler-previous
  run_runtime --consumer shioaji --ecr-registry "$ECR_REGISTRY" --docker-login -- \
    "$provider_helper" shioaji "$SHIOAJI_IMAGE_REF" \
    "$shioaji_state" "$shioaji_state/state.sqlite3" \
    findb-fetcher-shioaji-scheduler findb-fetcher-shioaji-scheduler-candidate findb-fetcher-shioaji-scheduler-previous \
    findb-fetcher-shioaji-scheduler-preflight /var/lib/findb-shioaji-fetcher/cache \
    findb-fetch-shioaji-scheduler --manifest "$shioaji_manifest"
fi

if [ "$FULL_MARKET_ENABLED" = false ] && [ "$FETCHER_DEPLOY_MODE" = activate ]; then
  for runtime in findb-full-market-twelve-data findb-full-market-finlab findb-full-market-shioaji findb-full-market-taifex; do
    if runtime_exists "$runtime"; then
      docker start "$runtime" >/dev/null
    fi
  done
fi

if [ "$FETCHER_DEPLOY_MODE" = candidate ]; then
  rollback_processed
  trap - ERR INT TERM HUP
  echo "fetcher_aws_deploy=candidate_ready_for_acceptance providers=$provider_count profile=$FETCHER_RUNTIME_PROFILE"
  exit 0
fi

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
  for backup in ${legacy_previous_backups[@]+"${legacy_previous_backups[@]}"}; do
    id=-; image=-
    for legacy_entry in ${legacy_moves[@]+"${legacy_moves[@]}"}; do
      IFS='|' read -r legacy_original full original_id legacy_running original_image <<< "$legacy_entry"
      if [ "$full" = "$backup" ]; then id="$original_id"; image="$original_image"; break; fi
    done
    [ "$id" != - ] && [ "$image" != - ] || return 1
    actual_id="$(docker inspect --format '{{.Id}}' "$backup")" || return 1
    actual_image="$(docker inspect --format '{{.Config.Image}}' "$backup")" || return 1
    [ "$actual_id" = "$id" ] && [ "$actual_image" = "$image" ] || return 1
    require_retired_runtime "$backup" || return 1
    commit_removals+=("${backup}|${id}|${image}|${backup}")
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
echo "fetcher_aws_deploy=activated providers=$provider_count profile=$FETCHER_RUNTIME_PROFILE"
