#!/usr/bin/env bash
# findb_environment_contract=app-environment-v1
# Release one Fetcher provider using credentials loaded by the caller.

set -euo pipefail
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

set +x

: "${AWS_REGION:?AWS_REGION is required}"
if [ "$AWS_REGION" != "ap-southeast-1" ]; then
  echo "release_fetcher_provider=failed reason=region_invalid" >&2
  exit 1
fi

provider="${1:?provider required}"
image="${2:?image required}"
state_dir="${3:?state directory required}"
state_path="${4:?state path required}"
stable="${5:?stable container required}"
candidate="${6:?candidate container required}"
previous="${7:?previous container required}"
preflight_name="${8:?preflight container required}"
cache_dir="${9:?cache directory or - required}"
shift 9
[ "$#" -gt 0 ] || { echo "release_fetcher_provider=failed reason=scheduler_command_missing" >&2; exit 2; }

provider_release_mode="${FETCHER_PROVIDER_RELEASE_MODE:-legacy}"
case "$provider_release_mode" in
  legacy|transactional) ;;
  *)
    echo "release_fetcher_provider=failed reason=release_mode_invalid" >&2
    exit 1
    ;;
esac
accepted_release=true
if [ "$provider_release_mode" = transactional ]; then
  case "${FETCHER_DEPLOY_MODE:-}" in
    candidate) accepted_release=false ;;
    activate) accepted_release=true ;;
    *)
      echo "release_fetcher_provider=failed reason=deploy_mode_invalid" >&2
      exit 1
      ;;
  esac
fi

case "$provider" in
  taifex)
    if ! { [ "${FETCHER_RUNTIME_PROFILE:-bounded}" = full-market ]; } && ! { [ "${FETCHER_RUNTIME_PROFILE:-bounded}" = bounded ] && [ "${APP_ENVIRONMENT:-}" = staging ]; }; then
      echo "release_fetcher_provider=failed reason=taifex_profile_invalid" >&2
      exit 1
    fi
    marker_identity="taifex"
    image_repository_suffix="twelve-data"
    source_key="${FETCHER_TAIFEX_SOURCE_CLIENT_KEY:-}"
    provider_key=""
    provider_env=(--env TAIFEX_BASE_URL --env TAIFEX_TIMEOUT_SECONDS --env TAIFEX_MAX_RESPONSE_BYTES)
    ;;
  twelve-data)
    marker_identity="twelve"
    image_repository_suffix="twelve-data"
    source_key="${FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY:-}"
    provider_key="${TWELVE_DATA_API_KEY:-}"
    provider_env=(
      --env TWELVE_DATA_API_KEY
      --env TWELVE_DATA_BASE_URL
      --env TWELVE_DATA_TIMEOUT_SECONDS
      --env TWELVE_DATA_MAX_RESPONSE_BYTES
    )
    ;;
  finlab)
    marker_identity="finlab"
    image_repository_suffix="finlab"
    source_key="${FETCHER_FINLAB_SOURCE_CLIENT_KEY:-}"
    provider_key="${FINLAB_API_TOKEN:-}"
    provider_env=(--env FINLAB_API_TOKEN)
    ;;
  shioaji)
    marker_identity="shioaji"
    image_repository_suffix="shioaji"
    if [ "${SHIOAJI_SIMULATION:-}" != "true" ]; then
      echo "release_fetcher_provider=failed reason=shioaji_simulation_required" >&2
      exit 1
    fi
    source_key="${FETCHER_SHIOAJI_SOURCE_CLIENT_KEY:-}"
    provider_key="${SHIOAJI_API_KEY:-}"
    provider_secret_key="${SHIOAJI_SECRET_KEY:-}"
    provider_env=(--env SHIOAJI_API_KEY --env SHIOAJI_SECRET_KEY --env SHIOAJI_SIMULATION)
    ;;
  *)
    echo "release_fetcher_provider=failed reason=provider_not_allowed" >&2
    exit 1
    ;;
esac

: "${APP_ENVIRONMENT:?APP_ENVIRONMENT is required}"
if [ "${DEPLOYMENT_TARGET+x}" = x ] && [ "$DEPLOYMENT_TARGET" != "$APP_ENVIRONMENT" ]; then
  echo "application_environment=failed reason=legacy_environment_conflict" >&2
  exit 1
fi

if [ "${ECR_REGISTRY:-}" != "${AWS_ACCOUNT_ID:-}.dkr.ecr.ap-southeast-1.amazonaws.com" ] \
  || [[ ! "${APP_ENVIRONMENT:-}" =~ ^(staging|production)$ ]]; then
  echo "release_fetcher_provider=failed reason=aws_route_invalid" >&2
  exit 1
fi
expected_image_repository="$ECR_REGISTRY/findb/$APP_ENVIRONMENT/fetcher/$image_repository_suffix"

if ! printf '%s' "$image" | grep -Eq "^${expected_image_repository}@sha256:[0-9a-f]{64}$"; then
  echo "release_fetcher_provider=failed reason=ecr_image_contract" >&2
  exit 1
fi

require_value() {
  [ -n "$2" ] || { echo "release_fetcher_provider=failed reason=required_value_missing" >&2; exit 1; }
}

require_value source_api_url "${FETCHER_SOURCE_API_URL:-}"
require_value source_client_key "$source_key"
if [ "$provider" != taifex ]; then
  require_value provider_credential "$provider_key"
fi
if [ "$provider" = "shioaji" ]; then
  require_value provider_secret_credential "$provider_secret_key"
fi
require_value serve_base_url "${FINDB_SERVE_BASE_URL:-}"
require_value calendar_key "${FETCHER_CALENDAR_SERVE_API_KEY:-}"
require_value r2_account "${CLOUDFLARE_R2_ACCOUNT_ID:-}"
require_value r2_bucket "${CLOUDFLARE_R2_RAW_BUCKET:-}"
require_value r2_access_key "${CLOUDFLARE_R2_RAW_ACCESS_KEY_ID:-}"
require_value r2_secret_key "${CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY:-}"

if [ "$source_key" = "$FETCHER_CALENDAR_SERVE_API_KEY" ]; then
  echo "release_fetcher_provider=failed reason=credential_reuse" >&2
  exit 1
fi
if ! printf '%s' "$CLOUDFLARE_R2_ACCOUNT_ID" | grep -Eq '^[0-9a-f]{32}$'; then
  echo "release_fetcher_provider=failed reason=r2_account_invalid" >&2
  exit 1
fi
if ! printf '%s' "$CLOUDFLARE_R2_RAW_BUCKET" | grep -Eq '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$' \
  || printf '%s' "$CLOUDFLARE_R2_RAW_BUCKET" | grep -Eq '^([0-9]{1,3}[.]){3}[0-9]{1,3}$'; then
  echo "release_fetcher_provider=failed reason=r2_bucket_invalid" >&2
  exit 1
fi

export SOURCE_API_URL="$FETCHER_SOURCE_API_URL"
export SOURCE_CLIENT_KEY="$source_key"
export FETCHER_STATE_PATH="$state_path"
export FETCHER_SHIOAJI_STATE_PATH="$state_path"

runtime_env_args=(
  --env FULL_MARKET_ENABLED
  --env FETCHER_CONSUMER_PROFILE
  --env FETCHER_ACCOUNT_STATE_PATH
  --env "FETCHER_ACCOUNT_READINESS_FILE=/var/lib/findb-account/readiness/${provider//-/_}.json"
  --env APP_ENVIRONMENT
  --env SOURCE_API_URL
  --env SOURCE_CLIENT_KEY
  --env FINDB_SERVE_BASE_URL
  --env FETCHER_CALENDAR_SERVE_API_KEY
  --env FETCHER_CALENDAR_TIMEOUT_SECONDS
  --env FETCHER_CALENDAR_CACHE_TTL_SECONDS
  --env CLOUDFLARE_R2_ACCOUNT_ID
  --env CLOUDFLARE_R2_RAW_BUCKET
  --env CLOUDFLARE_R2_RAW_ACCESS_KEY_ID
  --env CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY
  --env CLOUDFLARE_R2_RAW_SESSION_TOKEN
  --env CLOUDFLARE_R2_MAX_OBJECT_BYTES
  --env FETCHER_REQUEST_TIMEOUT_SECONDS
  --env FETCHER_SCHEDULER_CONTROL_POLL_SECONDS
  --env FETCHER_MAX_ATTEMPTS
  --env FETCHER_MAX_RETRY_AFTER_SECONDS
  --env FETCHER_STATE_PATH
  "${provider_env[@]}"
)
if [ "$provider" = "shioaji" ]; then
  runtime_env_args+=(--env FETCHER_SHIOAJI_STATE_PATH)
fi
# Only retained compatible Full images in an accepted bounded replay need the
# old input. Ordinary current images receive APP_ENVIRONMENT exclusively.
if [ "${FETCHER_LEGACY_ENV_BRIDGE:-false}" = true ]; then
  if [ "${FETCHER_ACCEPTED_REPLAY:-false}" != true ] || [ "${FETCHER_CONSUMER_PROFILE:-pilot}" != full_market ] || [ "${FETCHER_DEPLOY_MODE:-candidate}" != activate ]; then
    echo "application_environment=failed reason=legacy_bridge_not_accepted_replay" >&2
    exit 1
  fi
  runtime_env_args+=(--env "DEPLOYMENT_TARGET=$APP_ENVIRONMENT")
fi


sudo mkdir -p "$state_dir"
sudo chown 10001:10001 "$state_dir"
sudo chmod 0700 "$state_dir"
if [ "$(sudo stat -c '%u:%g:%a' "$state_dir")" != "10001:10001:700" ]; then
  echo "release_fetcher_provider=failed reason=state_directory_metadata" >&2
  exit 1
fi
if [ -e "$state_path" ] && { [ ! -f "$state_path" ] || [ "$(sudo stat -c '%u:%g:%a' "$state_path")" != "10001:10001:600" ]; }; then
  echo "release_fetcher_provider=failed reason=sqlite_metadata" >&2
  exit 1
fi
if [ "$cache_dir" != "-" ]; then
  sudo mkdir -p "$cache_dir"
  sudo chown 10001:10001 "$cache_dir"
  sudo chmod 0700 "$cache_dir"
  if [ "$(sudo stat -c '%u:%g:%a' "$cache_dir")" != "10001:10001:700" ]; then
    echo "release_fetcher_provider=failed reason=cache_metadata" >&2
    exit 1
  fi
fi

readiness_mount=()
account_mount=()
account_dir="/var/lib/findb-account/$APP_ENVIRONMENT"
if [ "${FULL_MARKET_ENABLED:-false}" = true ] || [ -d "$account_dir" ]; then
  sudo mkdir -p "$account_dir" /etc/findb-full-market/readiness
  sudo chown 10001:10001 "$account_dir"
  sudo chmod 0700 "$account_dir"
  account_mount=(--mount "type=bind,src=$account_dir,dst=$account_dir" --mount "type=bind,src=/etc/findb-full-market/readiness,dst=/var/lib/findb-account/readiness,readonly")
fi
if [ "${FETCHER_CONSUMER_PROFILE:-pilot}" = full_market ]; then
  # The official TW ISIN full universe exceeds the bounded 8 MiB default.
  # Keep the expanded raw/response ceiling explicit and profile-scoped.
  export CLOUDFLARE_R2_MAX_OBJECT_BYTES=16777216
  export TWELVE_DATA_MAX_RESPONSE_BYTES=16777216
  export TAIFEX_MAX_RESPONSE_BYTES=16777216
  # Readiness is operator evidence, never writable by the provider process.
  readiness_dir=/etc/findb-full-market/readiness
  sudo mkdir -p "$readiness_dir"
  [ ! -L "$readiness_dir" ] || { echo "release_fetcher_provider=failed reason=readiness_directory_unsafe" >&2; exit 1; }
  sudo chown 0:0 "$readiness_dir"
  sudo chmod 0755 "$readiness_dir"
  [ "$(sudo stat -c '%u:%g:%a' "$readiness_dir")" = "0:0:755" ] || {
    echo "release_fetcher_provider=failed reason=readiness_directory_metadata" >&2
    exit 1
  }
  readiness_mount=(--mount "type=bind,src=$readiness_dir,dst=$state_dir/readiness,readonly")
fi

raw_marker="$state_dir/raw-bucket.sha256"
if [ "${FETCHER_CONSUMER_PROFILE:-pilot}" = full_market ]; then
  # All four providers share only the quota/checkpoint DB, never credentials.
  marker_identity="full-market"
fi
fingerprint="$(printf '%s\n%s\n%s' "$CLOUDFLARE_R2_ACCOUNT_ID" "$CLOUDFLARE_R2_RAW_BUCKET" "$marker_identity" | sha256sum | cut -d ' ' -f1)"
write_marker() {
  marker_tmp="$(sudo mktemp "$state_dir/.raw-bucket.sha256.XXXXXX")"
  printf '%s\n' "$fingerprint" | sudo tee "$marker_tmp" >/dev/null
  sudo chown 10001:10001 "$marker_tmp"
  sudo chmod 0600 "$marker_tmp"
  sudo mv "$marker_tmp" "$raw_marker"
}
if sudo test -e "$state_path"; then
  if ! sudo test -f "$raw_marker" || [ "$(sudo stat -c '%u:%g:%a' "$raw_marker")" != "10001:10001:600" ]; then
    echo "release_fetcher_provider=failed reason=raw_marker_metadata" >&2
    exit 1
  fi
  if [ "$(sudo cat "$raw_marker")" != "$fingerprint" ]; then
    echo "release_fetcher_provider=failed reason=raw_bucket_mismatch" >&2
    exit 1
  fi
elif sudo test -e "$raw_marker"; then
  if [ "$(sudo cat "$raw_marker")" != "$fingerprint" ]; then
    write_marker
  fi
else
  write_marker
fi

# Resolve all entry identities before interrupted-candidate cleanup. Unknown
# inventory must not mutate even an unaccepted runtime before failing closed.
for entry_name in "$stable" "$previous" "$candidate"; do
  if runtime_exists "$entry_name"; then :; fi
done
recovery_original_id=-
remove_recovery_runtime() {
  local name="$1" id running exit_code oom error
  if ! runtime_exists "$name"; then return 0; fi
  id="$(docker inspect --format '{{.Id}}' "$name")" || return 1
  if [ -z "$id" ] || [ "$id" = "$recovery_original_id" ]; then return 1; fi
  running="$(docker inspect --format '{{.State.Running}}' "$name")" || return 1
  case "$running" in true|false) ;; *) return 1 ;; esac
  if [ "$running" = true ]; then docker stop --time 30 "$name" >/dev/null || return 1; fi
  running="$(docker inspect --format '{{.State.Running}}' "$name")" || return 1
  exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$name")" || return 1
  oom="$(docker inspect --format '{{.State.OOMKilled}}' "$name")" || return 1
  error="$(docker inspect --format '{{.State.Error}}' "$name")" || return 1
  if [ "$running" != false ] || [ "$exit_code" != 0 ] || [ "$oom" != false ] || [ -n "$error" ]; then
    echo "release_fetcher_provider=failed reason=runtime_retirement_failed runtime=$name" >&2
    return 1
  fi
  [ "$(docker inspect --format '{{.Id}}' "$name")" = "$id" ] || return 1
  docker rm "$name" >/dev/null || return 1
}

if runtime_exists "$candidate"; then
  # A candidate is never accepted state. A prior interrupted command may have
  # left it running before health validation. Retire it without killing work.
  remove_recovery_runtime "$candidate"
fi
if runtime_exists "$stable"; then
  stable_accepted="$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$stable")"
  if [ "$stable_accepted" = false ]; then
    # A hard interruption may occur after candidate -> stable but before the
    # coordinator rolls candidate mode back. It is never an accepted release.
    remove_recovery_runtime "$stable"
  fi
fi
if runtime_exists "$previous"; then
  if runtime_exists "$stable"; then
    previous_accepted="$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$previous")" || exit 1
    case "$previous_accepted" in true|false|''|'<no value>') ;; *) exit 1 ;; esac
    if [ "$previous_accepted" != false ]; then
      echo "release_fetcher_provider=failed reason=accepted_previous_unjournaled" >&2
      exit 1
    fi
    remove_recovery_runtime "$previous"
  else
    docker rename "$previous" "$stable"
    docker start "$stable" >/dev/null
  fi
fi

docker image prune -af
docker pull "$image"
cache_mount=()
if [ "$cache_dir" != "-" ]; then
  cache_mount=(--mount "type=bind,src=$cache_dir,dst=/home/fetcher")
fi

state_bootstrap=false
case "${FETCHER_CONSUMER_PROFILE:-pilot}:$1" in
  full_market:findb-fetch-full-market)
    if [ "${FETCHER_RECORDED_RELEASE_PROFILE:-${FETCHER_RUNTIME_PROFILE:-bounded}}" = full-market ]; then state_bootstrap=true; fi
    ;;
  pilot:findb-fetch-shioaji-scheduler)
    if [ "$provider" = shioaji ]; then state_bootstrap=true; fi
    ;;
  full_market:*|*:findb-fetch-full-market)
    echo "release_fetcher_provider=failed reason=consumer_command_mismatch" >&2
    exit 2
    ;;
esac
if [ "$state_bootstrap" = true ] && ! sudo test -e "$state_path"; then
  versioned_cutover=false
  if [ "${APP_ENVIRONMENT:-}" = staging ] && [ "$state_dir" = /var/lib/findb-shioaji-fetcher/staging-pilot-v3 ] && [ "${FETCHER_RUNTIME_PROFILE:-bounded}" = bounded ]; then
    if ! runtime_exists "$stable"; then
      versioned_cutover=true
    else
      stable_mounts="$(docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "$stable")" || exit 1
      if ! grep -Fxq "$state_dir" <<< "$stable_mounts"; then
        versioned_cutover=true
      fi
    fi
  fi
  if [ "${FETCHER_RUNTIME_PROFILE:-bounded}" = bounded ] && [ "$versioned_cutover" != true ] && runtime_exists "$stable"; then
    echo "release_fetcher_provider=failed reason=shioaji_state_missing_with_stable" >&2
    exit 1
  fi
  state_bootstrap_name="${preflight_name}-state"
  if runtime_exists "$state_bootstrap_name"; then
    remove_recovery_runtime "$state_bootstrap_name"
  fi
  # A fresh host has no SQLite file yet, while --require-stopped is
  # intentionally read-only and requires one. Create only the reviewed schema
  # before the stopped-state probe; this mode never constructs network clients.
  docker run --rm \
    --name "$state_bootstrap_name" \
    --env "APP_ENVIRONMENT=$APP_ENVIRONMENT" \
    --user 10001:10001 \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,size=16m \
    ${account_mount[@]+"${account_mount[@]}"} \
    --mount "type=bind,src=$state_dir,dst=$state_dir" \
    ${cache_mount[@]+"${cache_mount[@]}"} \
    ${readiness_mount[@]+"${readiness_mount[@]}"} \
    "$image" "$@" --state-path "$state_path" --initialize-state
  if [ ! -f "$state_path" ] \
    || [ "$(sudo stat -c '%u:%g:%a' "$state_path")" != "10001:10001:600" ]; then
    echo "release_fetcher_provider=failed reason=shioaji_state_bootstrap" >&2
    exit 1
  fi
fi

docker run --rm \
  --name "$preflight_name" \
  --user 10001:10001 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --mount "type=bind,src=$state_dir,dst=$state_dir" \
  ${cache_mount[@]+"${cache_mount[@]}"} \
  ${readiness_mount[@]+"${readiness_mount[@]}"} \
  "${runtime_env_args[@]}" \
  "$image" "$@" --check

recovery_original_id=-
recovery_original_running=false
if runtime_exists "$stable"; then
  recovery_original_id="$(docker inspect --format '{{.Id}}' "$stable")" || exit 1
  recovery_original_running="$(docker inspect --format '{{.State.Running}}' "$stable")" || exit 1
  [ -n "$recovery_original_id" ] || exit 1
  case "$recovery_original_running" in true|false) ;; *) exit 1 ;; esac
fi

recover() {
  status="${1:-$?}"
  [ "$status" -ne 0 ] || status=1
  set +e
  trap - ERR INT TERM HUP
  runtime_inventory_recovery=helper_recovery
  recovery_failed=0
  remove_recovery_runtime "${stable}-historical" || recovery_failed=1
  remove_recovery_runtime "$candidate" || recovery_failed=1
  if runtime_exists "$previous"; then
    previous_id="$(docker inspect --format '{{.Id}}' "$previous")" || recovery_failed=1
    if [ "$previous_id" != "$recovery_original_id" ] || [ "$recovery_original_id" = - ]; then
      recovery_failed=1
    elif [ "$recovery_failed" -eq 0 ]; then
      remove_recovery_runtime "$stable" || recovery_failed=1
      if [ "$recovery_failed" -eq 0 ]; then docker rename "$previous" "$stable" >/dev/null || recovery_failed=1; fi
    fi
  elif [ "$recovery_original_id" = - ]; then
    remove_recovery_runtime "$stable" || recovery_failed=1
  fi
  if [ "$recovery_failed" -eq 0 ] && [ "$recovery_original_id" != - ]; then
    if ! runtime_exists "$stable" \
      || [ "$(docker inspect --format '{{.Id}}' "$stable")" != "$recovery_original_id" ]; then
      recovery_failed=1
    else
      recovery_current="$(docker inspect --format '{{.State.Running}}' "$stable")" || recovery_failed=1
      case "$recovery_current" in true|false) ;; *) recovery_failed=1 ;; esac
      if [ "$recovery_failed" -eq 0 ]; then
        if [ "$recovery_original_running" = true ] && [ "$recovery_current" = false ]; then
          docker start "$stable" >/dev/null || recovery_failed=1
        elif [ "$recovery_original_running" = false ] && [ "$recovery_current" = true ]; then
          docker stop --time 30 "$stable" >/dev/null || recovery_failed=1
          recovery_current="$(docker inspect --format '{{.State.Running}}' "$stable")" || recovery_failed=1
          recovery_exit="$(docker inspect --format '{{.State.ExitCode}}' "$stable")" || recovery_failed=1
          recovery_oom="$(docker inspect --format '{{.State.OOMKilled}}' "$stable")" || recovery_failed=1
          recovery_error="$(docker inspect --format '{{.State.Error}}' "$stable")" || recovery_failed=1
          if [ "$recovery_current" != false ] || [ "$recovery_exit" != 0 ] \
            || [ "$recovery_oom" != false ] || [ -n "$recovery_error" ]; then recovery_failed=1; fi
        fi
      fi
    fi
  fi
  [ "$recovery_failed" -eq 0 ] || echo "release_fetcher_provider=failed reason=recovery_failed" >&2
  exit "$status"
}
trap 'recover "$?"' ERR
trap 'recover 130' INT
trap 'recover 143' TERM
trap 'recover 129' HUP
runtime_inventory_recovery=helper

had_previous=0
if runtime_exists "$stable"; then
  had_previous=1
  stable_running="$(docker inspect --format '{{.State.Running}}' "$stable")" || false
  case "$stable_running" in true|false) ;; *) false ;; esac
  if [ "$stable_running" = true ]; then docker stop --time 30 "$stable" >/dev/null; fi
  stable_running="$(docker inspect --format '{{.State.Running}}' "$stable")" || false
  stable_exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$stable")" || false
  stable_oom="$(docker inspect --format '{{.State.OOMKilled}}' "$stable")" || false
  stable_error="$(docker inspect --format '{{.State.Error}}' "$stable")" || false
  if [ "$stable_running" != false ] || [ "$stable_exit_code" != 0 ] \
    || [ "$stable_oom" != false ] || [ -n "$stable_error" ]; then
    echo "release_fetcher_provider=failed reason=graceful_stop" >&2
    false
  fi
  [ "$(docker inspect --format '{{.Id}}' "$stable")" = "$recovery_original_id" ] || false
  docker rename "$stable" "$previous"
fi

# The deployment identity only reports the already-stopped observation and
# reads DB desired state. It never changes desired state; Dashboard Owner must
# stop all providers before dispatch and re-enable approved providers later.
docker run --rm \
  --name "${preflight_name}-control" \
  --user 10001:10001 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --mount "type=bind,src=$state_dir,dst=$state_dir" \
  ${cache_mount[@]+"${cache_mount[@]}"} \
  ${readiness_mount[@]+"${readiness_mount[@]}"} \
  "${runtime_env_args[@]}" \
  "$image" "$@" --require-stopped

common_args=(
  --user 10001:10001
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges
  --log-opt max-size=10m
  --log-opt max-file=3
  --tmpfs /tmp:rw,noexec,nosuid,size=16m
  --mount "type=bind,src=$state_dir,dst=$state_dir"
  ${account_mount[@]+"${account_mount[@]}"}
)
if [ "$cache_dir" != "-" ]; then
  common_args+=(--mount "type=bind,src=$cache_dir,dst=/home/fetcher")
fi
if [ "${FETCHER_CONSUMER_PROFILE:-pilot}" = full_market ]; then
  common_args+=("${readiness_mount[@]}")
fi
if [ "$accepted_release" = false ]; then
  # Candidate mode has already executed the CLI's offline check and strict
  # stopped-state probe. Validate the final container configuration without
  # ever starting an unaccepted scheduler process.
  docker create --name "$candidate" --label "com.findb.fetcher.accepted=false" \
    --restart no "${common_args[@]}" "${runtime_env_args[@]}" \
    "$image" "$@" --run-forever >/dev/null
  candidate_image="$(docker inspect --format '{{.Config.Image}}' "$candidate")"
  candidate_user="$(docker inspect --format '{{.Config.User}}' "$candidate")"
  candidate_restart="$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$candidate")"
  if [ "$candidate_image" != "$image" ] || [ "$candidate_user" != "10001:10001" ] \
    || [ "$candidate_restart" != no ]; then
    echo "release_fetcher_provider=failed reason=candidate_config_invalid" >&2
    false
  fi
  docker rm "$candidate" >/dev/null
  trap - ERR INT TERM HUP
  echo "release_fetcher_provider=ready provider=$provider mode=$provider_release_mode previous=$had_previous"
  exit 0
fi
docker run -d --name "$candidate" --label "com.findb.fetcher.accepted=$accepted_release" \
  --restart unless-stopped "${common_args[@]}" "${runtime_env_args[@]}" \
  "$image" "$@" --run-forever >/dev/null
candidate_image="$(docker inspect --format '{{.Config.Image}}' "$candidate")"
candidate_user="$(docker inspect --format '{{.Config.User}}' "$candidate")"
healthy=0
for attempt in 1 2 3 4 5 6; do
  status="$(docker inspect --format '{{.State.Status}}' "$candidate")"
  restarts="$(docker inspect --format '{{.RestartCount}}' "$candidate")"
  if [ "$status" != "running" ] || [ "$restarts" -ne 0 ]; then
    break
  fi
  if [ "$attempt" -eq 6 ]; then
    healthy=1
    break
  fi
  sleep 5
done
if [ "$candidate_image" != "$image" ] || [ "$candidate_user" != "10001:10001" ] || [ "$healthy" -ne 1 ]; then
  echo "release_fetcher_provider=failed reason=candidate_unhealthy" >&2
  false
fi
docker rename "$candidate" "$stable"
if [ "$(docker inspect --format '{{.State.Running}}' "$stable")" != "true" ]; then
  echo "release_fetcher_provider=failed reason=stable_not_running" >&2
  false
fi

# This process runs *inside* runtime_secret_command, after exactly one
# provider consumer's secret set has been loaded.  Reuse only that already
# allowlisted ``runtime_env_args`` array; the outer deployment shell never
# receives SOURCE_CLIENT_KEY/provider/R2 credentials.
if [ "${FETCHER_CONSUMER_PROFILE:-pilot}" = full_market ] || [ "$APP_ENVIRONMENT" = staging ]; then
  trap - ERR INT TERM HUP
  echo "release_fetcher_provider=ready provider=$provider mode=$provider_release_mode previous=$had_previous"
  exit 0
fi
case "$provider" in
  twelve-data) historical_provider=twelve_data ;;
  finlab) historical_provider=finlab ;;
  shioaji) historical_provider=shioaji ;;
esac
historical_name="${stable}-historical"
historical_state_dir="$state_dir/historical"
sudo mkdir -p "$historical_state_dir"
sudo chown 10001:10001 "$historical_state_dir"
sudo chmod 0700 "$historical_state_dir"
remove_recovery_runtime "$historical_name"
docker run -d --name "$historical_name" --label "com.findb.fetcher.accepted=$accepted_release" \
  --label com.findb.fetcher.historical-shutdown=date-boundary-v1 \
  --restart unless-stopped "${common_args[@]}" "${runtime_env_args[@]}" \
  --env FETCHER_CONSUMER_PROFILE=historical \
  --env "FETCHER_HISTORICAL_STATE_DIR=$historical_state_dir" \
  "$image" findb-fetch-historical-backfill --provider "$historical_provider" --run-forever >/dev/null
if [ "$(docker inspect --format '{{.State.Running}}' "$historical_name")" != "true" ]; then
  echo "release_fetcher_provider=failed reason=historical_not_running" >&2
  false
fi
if [ "$provider_release_mode" = legacy ] && [ "$had_previous" -eq 1 ]; then
  docker rm "$previous" >/dev/null
fi
trap - ERR INT TERM HUP
echo "release_fetcher_provider=ready provider=$provider mode=$provider_release_mode previous=$had_previous"
