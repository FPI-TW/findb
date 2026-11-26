#!/usr/bin/env bash
# findb_environment_contract=app-environment-v1
# Transactional Fetcher staging deployment. Secrets are loaded independently
# for each provider by the instance role and never cross this host boundary.

set -Eeuo pipefail
# A failed inspect is not proof of absence: Docker uses rc=1 for daemon and
# permission errors too. Only a successful exact-name inventory can prove absence.
runtime_inventory_recovery=none
# Same bound as the provider helper: a slow graceful exit must not become SIGKILL.
runtime_stop_timeout_seconds=90
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
        "findb-fetch-historical-backfill": "historical",
    }
    if command == ["python", "-m", "findb_fetcher"]:
        role = "readiness"
    elif command[0] == "findb-fetch-runtime":
        role = command[command.index("--execution-profile") + 1]
        if role != "full_market":
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

if [ "${PROVIDER_WORK_REQUIRED:-true}" != true ]; then
  echo "fetcher_aws_deploy=failed reason=legacy_provider_execution_retired" >&2
  exit 1
fi

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
: "${FACTSET_IMAGE_REF:?FACTSET_IMAGE_REF is required}"

FETCHER_RUNTIME_PROFILE=unified
export FETCHER_RUNTIME_PROFILE
PROVIDER_WORK_REQUIRED="${PROVIDER_WORK_REQUIRED:-true}"
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
  candidate|activate|apply) ;;
  *) echo "fetcher_aws_deploy=failed reason=deploy_mode_invalid" >&2; exit 1 ;;
esac
  if ! grep -Eq '^/opt/fetcher/releases/[0-9a-f]{64}-[A-Za-z0-9._-]+$' <<<"$FETCHER_RELEASE_ROOT" \
  || [ ! -d "$FETCHER_RELEASE_ROOT" ] || [ -L "$FETCHER_RELEASE_ROOT" ] \
  || [ "$(stat -c '%U:%G:%a' "$FETCHER_RELEASE_ROOT")" != root:root:700 ]; then
  echo "fetcher_aws_deploy=failed reason=release_root_invalid" >&2
  exit 1
fi

provider_inventory="$(python3 - "$FETCHER_RELEASE_ROOT/infra/deploy/generated/provider_inventory.v1.json" <<'INVENTORY'
import hashlib, json, re, sys
try:
    value = json.load(open(sys.argv[1]))
    digest = value.pop("projection_sha256")
    if hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != digest:
        raise ValueError()
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError()
    consumers = value["consumers"]
    providers = value["providers"]
    if not providers or len(set(providers)) != len(providers):
        raise ValueError()
    names = [c["name"] for c in consumers]
    if len(set(names)) != len(consumers) or any(c["provider"] not in providers for c in consumers):
        raise ValueError()
    # The transaction treats NAME-* as replicas of NAME; no consumer may look like one.
    if any(other.startswith(name + "-") for name in names for other in names):
        raise ValueError()
    # Host state/cache layouts are checked in bash below; unknown providers fail there.
    for provider in providers:
        rows = [c for c in consumers if c["provider"] == provider]
        if any(type(c["lane"]) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,29}", c["lane"]) for c in rows):
            raise ValueError()
        # One historical and one default Full consumer, plus one Full consumer per other lane.
        defaults = sorted(c["role"] for c in rows if c["lane"] == "default")
        lanes = [c["lane"] for c in rows if c["lane"] != "default"]
        if defaults != ["full_market", "historical"] or len(set(lanes)) != len(lanes):
            raise ValueError()
        if any(c["role"] != "full_market" for c in rows if c["lane"] != "default"):
            raise ValueError()
        suffix = provider.replace("_", "-")
        for c in rows:
            lane = "" if c["lane"] == "default" else c["lane"].replace("_", "-") + "-"
            if c["name"] != "findb-" + c["role"].replace("_", "-") + "-" + lane + suffix:
                raise ValueError()
        if len({c["image_key"] for c in rows}) != 1 or rows[0]["image_key"] not in value["images"]:
            raise ValueError()
        print(suffix, rows[0]["image_key"], ",".join(sorted(lanes)) or "-")
except Exception:
    raise SystemExit("fetcher_aws_deploy=failed reason=provider_inventory_invalid") from None
INVENTORY
)"
providers=()
image_keys=()
seen_image_keys=" "
consumer_count=0
while read -r provider image_key lanes; do
  providers+=("$provider")
  consumer_count=$((consumer_count + 2))
  if [ "$lanes" != - ]; then
    IFS=, read -r -a provider_lane_list <<< "$lanes"
    consumer_count=$((consumer_count + ${#provider_lane_list[@]}))
  fi
  case "$seen_image_keys" in
    *" $image_key "*) ;;
    *) image_keys+=("$image_key"); seen_image_keys="$seen_image_keys$image_key " ;;
  esac
done <<< "$provider_inventory"
# The workflow passes one exact image reference per generated image key. These
# variable names are the workflow/transaction interface, not provider metadata.
image_ref() {
  case "$1" in
    twelve_data) printf '%s' "$TWELVE_IMAGE_REF" ;;
    finlab) printf '%s' "$FINLAB_IMAGE_REF" ;;
    factset) printf '%s' "$FACTSET_IMAGE_REF" ;;
    *) return 1 ;;
  esac
}
provider_image() {
  local provider image_key lanes
  while read -r provider image_key lanes; do
    if [ "$provider" = "$1" ]; then
      image_ref "$image_key"
      return
    fi
  done <<< "$provider_inventory"
  return 1
}
# A provider's non-default Full lanes (comma-separated, or - for none). The default
# lane keeps every pre-lane container, checkpoint and command.
provider_lanes() {
  local provider image_key lanes
  while read -r provider image_key lanes; do
    if [ "$provider" = "$1" ]; then
      printf '%s' "$lanes"
      return
    fi
  done <<< "$provider_inventory"
  return 1
}
# Full consumer names of a provider: default lane first, then each other lane.
provider_full_names() {
  local lanes lane
  lanes="$(provider_lanes "$1")" || return 1
  printf '%s\n' "findb-full-market-$1"
  [ "$lanes" != - ] || return 0
  for lane in ${lanes//,/ }; do
    printf '%s\n' "findb-full-market-${lane//_/-}-$1"
  done
}
# Existing durable host directories (historical state, optional SDK cache). They
# are adapter-owned on-disk locations, so a catalog edit can never move state.
provider_host_layout() {
  case "$1" in
    twelve-data) printf '%s %s' /var/lib/findb-fetcher/historical - ;;
    finlab) printf '%s %s' /var/lib/findb-finlab-fetcher/historical /var/lib/findb-finlab-fetcher/cache ;;
    taifex) printf '%s %s' /var/lib/findb-taifex-fetcher/historical - ;;
    factset) printf '%s %s' /var/lib/findb-factset-fetcher/historical - ;;
    hkex) printf '%s %s' /var/lib/findb-hkex-fetcher/historical - ;;
    fmp) printf '%s %s' /var/lib/findb-fmp-fetcher/historical - ;;
    *) return 1 ;;
  esac
}
for provider in "${providers[@]}"; do
  provider_host_layout "$provider" >/dev/null || {
    echo "fetcher_aws_deploy=failed reason=provider_inventory_invalid" >&2
    exit 1
  }
done
for image_key in "${image_keys[@]}"; do
  image_ref "$image_key" >/dev/null || {
    echo "fetcher_aws_deploy=failed reason=provider_inventory_invalid" >&2
    exit 1
  }
done

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
  grep -Eq "^${expected_registry}/${repository}@sha256:[0-9a-f]{64}$" <<<"$image" || {
    echo "fetcher_aws_deploy=failed reason=ecr_image_contract" >&2
    exit 1
  }
}
image_refs=()
for image_key in "${image_keys[@]}"; do
  image="$(image_ref "$image_key")"
  validate_image "$image" "findb/$APP_ENVIRONMENT/fetcher/${image_key//_/-}"
  image_refs+=("$image")
done

# Only non-secret deployment configuration is preserved. The wrapper loads one
# consumer's allowlisted values into /run and removes them after the child exits.
preserve_env=DEPLOYMENT_TRANSACTION_ID,PROVIDER_WORK_REQUIRED,FETCHER_CONSUMER_PROFILE,FETCHER_ACCOUNT_STATE_PATH,FETCHER_RUNTIME_PROFILE,AWS_REGION,AWS_ACCOUNT_ID,APP_ENVIRONMENT,ECR_REGISTRY,FETCHER_RELEASE_ROOT,FETCHER_DEPLOY_MODE,FETCHER_PROVIDER_RELEASE_MODE,FETCHER_IMAGES_PREPARED,FETCHER_HEALTH_MODE,TWELVE_IMAGE_REF,FINLAB_IMAGE_REF,FACTSET_IMAGE_REF
export FETCHER_PROVIDER_RELEASE_MODE=transactional
run_runtime() {
  local started=$SECONDS status=0 consumers="" previous="" argument
  for argument in "$@"; do
    [ "$argument" != -- ] || break
    if [ "$previous" = --consumer ]; then consumers="${consumers:+$consumers,}$argument"; fi
    previous="$argument"
  done
  sudo --preserve-env="$preserve_env" "$runtime_command" \
    --catalog "$catalog" --region "$AWS_REGION" --deployment-target "$APP_ENVIRONMENT" --aws-account-id "$AWS_ACCOUNT_ID" "$@" || status=$?
  printf 'deployment_runtime unit=fetcher consumers=%s seconds=%s status=%s\n' "$consumers" "$((SECONDS - started))" "$status"
  return "$status"
}

recorded_profile="$FETCHER_RUNTIME_PROFILE"
export FETCHER_RECORDED_RELEASE_PROFILE="$recorded_profile"
full_market_state=/var/lib/findb-full-market/$APP_ENVIRONMENT
[ "$APP_ENVIRONMENT" != production ] || full_market_state=/var/lib/findb-full-market
require_full_checkpoint() {
  # Installed workers own durable prepared work. Never replace a lost checkpoint,
  # including when an accepted replay invokes an older helper/image. Check the
  # provider lane's own file the installed runtime runs on (#691).
  local checkpoint_file
  if ! checkpoint_file="$(python3 - "$1" "$full_market_state" <<'STATE'
import json, re, sys
command, root = json.loads(sys.argv[1]), sys.argv[2]
path = command[command.index("--state-path") + 1] if "--state-path" in command else ""
if not re.fullmatch(re.escape(root) + r"/state-[a-z][a-z0-9_]{0,49}(-[a-z][a-z0-9_]{0,29})?\.sqlite3", path):
    raise SystemExit(1)
print(path)
STATE
)" || ! sudo test -f "$checkpoint_file" \
    || sudo test -L "$checkpoint_file" \
    || sudo test -L "$full_market_state"; then
    echo "fetcher_aws_deploy=failed reason=full_market_checkpoint_missing" >&2
    return 1
  fi
  if ! sudo python3 - "$checkpoint_file" <<'CHECKPOINT'
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
  if [ "$FETCHER_DEPLOY_MODE" = apply ]; then
    # This immutable, non-secret snapshot is used only before host mutation.
    # A missing/duplicate/malformed row is unknown state, never absence.
    initial_runtime_command="$(printf '%s' "$initial_runtime_snapshot" | python3 -c '
import json, sys
try:
    rows = [json.loads(line) for line in sys.stdin if line.strip()]
    matches = [row for row in rows if row["name"] == "/" + sys.argv[1]]
    if len(matches) != 1:
        raise ValueError("missing or duplicate runtime")
    row = matches[0]
    if (not all(isinstance(row[key], str) and row[key] for key in ("id", "image"))
        or type(row["running"]) is not bool or type(row["oom"]) is not bool
        or type(row["exit_code"]) is not int or row["exit_code"] < 0
        or not isinstance(row["error"], str)
        or row["accepted"] not in (None, "true", "false", "", "<no value>")):
        raise ValueError("invalid runtime metadata")
    print(json.dumps(row["command"]))
except Exception:
    sys.exit(1)
' "$name")" || runtime_inventory_failure
    runtime_command_role "$initial_runtime_command" >/dev/null || runtime_inventory_failure
    return 0
  fi
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
initial_runtime_snapshot=""
if [ "$FETCHER_DEPLOY_MODE" = apply ] && [ -n "$initial_runtime_inventory" ]; then
  initial_names=()
  while IFS= read -r name; do initial_names+=("$name"); done <<< "$initial_runtime_inventory"
  initial_runtime_snapshot="$(docker inspect --format '{"name":{{json .Name}},"id":{{json .Id}},"image":{{json .Config.Image}},"running":{{json .State.Running}},"exit_code":{{json .State.ExitCode}},"oom":{{json .State.OOMKilled}},"error":{{json .State.Error}},"accepted":{{json (index .Config.Labels "com.findb.fetcher.accepted")}},"command":{{json .Config.Cmd}}}' "${initial_names[@]}")" || runtime_inventory_failure
fi
initial_runtime_exists() {
  local requested="$1" name
  while IFS= read -r name; do
    if [ "$name" = "$requested" ]; then
      if [ "$FETCHER_DEPLOY_MODE" != apply ]; then
        docker container inspect "$requested" >/dev/null 2>&1 || runtime_inventory_failure
      fi
      return 0
    fi
  done <<< "$initial_runtime_inventory"
  return 1
}
retained_full_market=false
for provider in "${providers[@]}"; do
  full_names=()
  while IFS= read -r name; do full_names+=("$name"); done < <(provider_full_names "$provider")
  for base in "${full_names[@]}" "findb-historical-$provider"; do
    for installed in "$base" "${base}-previous" "${base}-candidate" "${base}-transaction-backup" "${base}-legacy-previous" "${base}-preflight" "${base}-preflight-state" "${base}-preflight-control"; do
      if initial_runtime_exists "$installed"; then
        initial_runtime_state "$installed"
        if [ "$FETCHER_DEPLOY_MODE" = apply ]; then
          installed_command="$initial_runtime_command"
        else
          installed_command="$(docker inspect --format '{{json .Config.Cmd}}' "$installed")" || runtime_inventory_failure
        fi
        installed_role="$(runtime_command_role "$installed_command")" || runtime_inventory_failure
        if [[ "$installed" == findb-full-market-* ]] || [ "$installed_role" = full_market ]; then
          require_full_checkpoint "$installed_command"
          retained_full_market=true
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
    docker stop --time "$runtime_stop_timeout_seconds" "$original" >/dev/null || return 1
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
      if [ "$running" = true ]; then
        docker stop --time "$runtime_stop_timeout_seconds" "$name" >/dev/null || return 1
        require_retired_runtime "$name" || return 1
      else
        require_discarded_runtime "$name" || return 1
      fi
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
    if [ "$running" = true ]; then docker stop --time "$runtime_stop_timeout_seconds" "$backup" >/dev/null || return 1; fi
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
  # The complete record policy applies equally to Full and historical.
  register_named_full "$1" "$2"
}

if [ "$FETCHER_DEPLOY_MODE" = activate ]; then
  if [ -e /opt/fetcher/current ] && [ ! -L /opt/fetcher/current ]; then
    echo "fetcher_aws_deploy=failed reason=current_pointer_invalid" >&2
    false
  fi
  if [ -L /opt/fetcher/current ]; then
    previous_pointer="$(readlink /opt/fetcher/current)"
    grep -Eq '^/opt/fetcher/releases/[0-9a-f]{64}-[A-Za-z0-9._-]+$' <<<"$previous_pointer" || {
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

# A runtime this transaction created and that ended by itself is the reason for
# the rollback, not state to preserve: its own non-zero exit (a rejected start,
# for instance) must not stop the originals from being restored. Only a process
# that returned on its own qualifies; one that was killed (a signal exit, OOM or
# a Docker error) still fails closed like any runtime that did not drain.
require_discarded_runtime() {
  local stable="$1" running exit_code oom error
  running="$(docker inspect --format '{{.State.Running}}' "$stable")" || return 1
  exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$stable")" || return 1
  oom="$(docker inspect --format '{{.State.OOMKilled}}' "$stable")" || return 1
  error="$(docker inspect --format '{{.State.Error}}' "$stable")" || return 1
  if [ "$running" = false ] && [ "$oom" = false ] && [ -z "$error" ] \
    && [[ "$exit_code" =~ ^[0-9]+$ ]] && [ "$exit_code" -lt 128 ]; then
    return 0
  fi
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

# -- end of transaction helpers; the release proper begins below --

# Pull each exact image once per phase with a secret-free consumer. Individual
# provider credentials remain confined to their own runtime wrapper invocation.
run_runtime --consumer images --ecr-registry "$ECR_REGISTRY" --docker-login -- \
  bash -s -- "${image_refs[@]}" <<'PREPARE_IMAGES'
set -euo pipefail
# Only image downloads overlap. Wait for every child, including on failure,
# before any provider mutation or Docker login cleanup.
pull_exact() {
  docker pull "$1" >/dev/null
  local digests
  digests="$(docker image inspect "$1" --format '{{range .RepoDigests}}{{println .}}{{end}}')"
  grep -Fxq -- "$1" <<<"$digests"
}
pids=()
for image in "$@"; do
  pull_exact "$image" & pids+=("$!")
done
failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=1
done
[ "$failed" -eq 0 ]
PREPARE_IMAGES
export FETCHER_IMAGES_PREPARED=true FETCHER_HEALTH_MODE=coordinator
activated_runtimes=()
record_activated_runtime() {
  local stable="$1" image="$2" id
  [ "$FETCHER_DEPLOY_MODE" != candidate ] || return 0
  id="$(docker inspect --format '{{.Id}}' "$stable")" || return 1
  [ -n "$id" ] || return 1
  activated_runtimes+=("$stable|$id|$image")
}
verify_activated_runtimes() {
  local attempt entry stable id image actual_id actual_image running restarts
  if [ "$FETCHER_DEPLOY_MODE" = apply ]; then
    python3 "$FETCHER_RELEASE_ROOT/infra/deploy/deployment_transaction.py" health --root "$FETCHER_RELEASE_ROOT" --observe-seconds 25
    return 0
  fi
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

# Every generated consumer: one historical and one Full consumer per provider lane.
provider_count="$consumer_count"
if [ "$FETCHER_DEPLOY_MODE" = apply ]; then
  for provider in "${providers[@]}"; do
    image="$(provider_image "$provider")"
    layout="$(provider_host_layout "$provider")"
    read -r history cache <<< "$layout"
    lanes="$(provider_lanes "$provider")"
    stables=()
    while IFS= read -r name; do stables+=("$name"); done < <(provider_full_names "$provider")
    stables+=("findb-historical-$provider")
    for stable in "${stables[@]}"; do
      register_provider "$stable" "${stable}-previous"
    done
    run_runtime --consumer "$provider" -- bash -s -- "$provider_helper" "$provider" "$image" "$full_market_state" "$history" "$cache" "$lanes" <<'PROVIDER_PROFILES'
set -euo pipefail
helper="$1"; provider="$2"; image="$3"; full="$4"; history="$5"; cache="$6"; lanes="$7"
# The default lane first, then each other Full lane, then historical (always default).
consumers=(full_market:default)
if [ "$lanes" != - ]; then
  for lane in ${lanes//,/ }; do consumers+=("full_market:$lane"); done
fi
consumers+=(historical:default)
for consumer in "${consumers[@]}"; do
  profile="${consumer%%:*}"; lane="${consumer#*:}"
  export FETCHER_CONSUMER_PROFILE="$profile"
  stable="findb-${profile//_/-}-$provider"
  [ "$lane" = default ] || stable="findb-${profile//_/-}-${lane//_/-}-$provider"
  case "$profile" in
    full_market) state="$full" ;;
    historical) state="$history" ;;
  esac
  # Each Full consumer owns its checkpoint file; historical keeps its own directory.
  checkpoint="$state/state.sqlite3"
  if [ "$profile" != historical ]; then
    checkpoint="$state/state-${provider//-/_}.sqlite3"
    [ "$lane" = default ] || checkpoint="$state/state-${provider//-/_}-$lane.sqlite3"
  fi
  command=(findb-fetch-runtime --execution-profile "$profile" --config "/app/configs/bootstrap.$APP_ENVIRONMENT.v1.json" --state-path "$checkpoint" --provider "${provider//-/_}")
  [ "$lane" = default ] || command+=(--lane "$lane")
  if [ "$profile" = historical ]; then command=(findb-fetch-historical-backfill --provider "${provider//-/_}"); fi
  "$helper" "$provider" "$image" "$state" "$checkpoint" "$stable" "$stable-candidate" "$stable-previous" "$stable-preflight" "$cache" "${command[@]}"
done
PROVIDER_PROFILES
    for stable in "${stables[@]}"; do
      record_activated_runtime "$stable" "$image"
    done
  done
else
export FETCHER_CONSUMER_PROFILE=full_market
for provider in "${providers[@]}"; do
  image="$(provider_image "$provider")"
  identity="${provider//-/_}"
  layout="$(provider_host_layout "$provider")"
  read -r _ cache_dir <<< "$layout"
  lanes="$(provider_lanes "$provider")"
  consumer_lanes=(default)
  [ "$lanes" = - ] || consumer_lanes+=(${lanes//,/ })
  for lane in "${consumer_lanes[@]}"; do
    stable="findb-full-market-$provider"
    checkpoint="$full_market_state/state-$identity.sqlite3"
    lane_args=()
    if [ "$lane" != default ]; then
      stable="findb-full-market-${lane//_/-}-$provider"
      checkpoint="$full_market_state/state-$identity-$lane.sqlite3"
      lane_args=(--lane "$lane")
    fi
    register_provider "$stable" "${stable}-previous"
    run_runtime --consumer "$provider" -- \
      "$provider_helper" "$provider" "$image" \
      "$full_market_state" "$checkpoint" \
      "$stable" "${stable}-candidate" "${stable}-previous" "${stable}-preflight" "$cache_dir" \
      findb-fetch-runtime --execution-profile full_market \
      --config "/app/configs/bootstrap.$APP_ENVIRONMENT.v1.json" \
      --state-path "$checkpoint" --provider "$identity" ${lane_args[@]+"${lane_args[@]}"}
    record_activated_runtime "$stable" "$image"
  done
done

# Historical requests retain their existing provider-specific durable directory.
# They are independently journaled consumers, never side effects of Full market release.
export FETCHER_CONSUMER_PROFILE=historical
# historical-taifex-v1: accepted installations require the TAIFEX historical consumer.
# factset-consumers-v1: accepted installations require the FactSet consumers.
# backend-schedules-v1: clocks are published by Source; bootstrap contains no schedules.
# shioaji-retired-v1: accepted installations require four providers and no Shioaji consumer.
# pilot-retired-v1: accepted installations require four providers and eight consumers.
# hkex-consumers-v1: accepted installations require the HKEX consumers (five providers, ten consumers).
# fmp-consumers-v1: accepted installations require the FMP consumers (six providers, twelve consumers).
# finlab-intraday-lane-v1: accepted installations require FinLab's intraday-lane consumer (six providers, thirteen consumers).
for provider in "${providers[@]}"; do
  image="$(provider_image "$provider")"
  layout="$(provider_host_layout "$provider")"
  read -r state_dir cache_dir <<< "$layout"
  stable="findb-historical-$provider"
  register_provider "$stable" "${stable}-previous"
  run_runtime --consumer "$provider" -- \
    "$provider_helper" "$provider" "$image" "$state_dir" "$state_dir/state.sqlite3" \
    "$stable" "${stable}-candidate" "${stable}-previous" "${stable}-preflight" "$cache_dir" \
    findb-fetch-historical-backfill --provider "${provider//-/_}"
  record_activated_runtime "$stable" "$image"
done

fi

if [ "$FETCHER_DEPLOY_MODE" = candidate ]; then
  rollback_processed
  trap - ERR INT TERM HUP
  echo "fetcher_aws_deploy=candidate_ready_for_acceptance providers=$provider_count profile=$FETCHER_RUNTIME_PROFILE"
  exit 0
fi

verify_activated_runtimes
if [ "$FETCHER_DEPLOY_MODE" = apply ]; then
  # Originals and current pointer remain intact until the runner decision.
  trap - ERR INT TERM HUP
  echo "fetcher_aws_deploy=healthy_pending_acceptance"
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
    if [ "$running" = true ]; then docker stop --time "$runtime_stop_timeout_seconds" "$backup" >/dev/null || return 1; fi
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
echo "fetcher_aws_deploy=activated providers=$provider_count profile=$FETCHER_RUNTIME_PROFILE"
