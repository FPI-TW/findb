#!/usr/bin/env bash
# Release one Fetcher provider using credentials loaded by the caller.

set -euo pipefail
set +x

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

case "$provider" in
  twelve-data)
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
    source_key="${FETCHER_FINLAB_SOURCE_CLIENT_KEY:-}"
    provider_key="${FINLAB_API_TOKEN:-}"
    provider_env=(--env FINLAB_API_TOKEN)
    ;;
  shioaji)
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

require_value() {
  [ -n "$2" ] || { echo "release_fetcher_provider=failed reason=required_value_missing" >&2; exit 1; }
}

require_value source_api_url "${FETCHER_SOURCE_API_URL:-}"
require_value source_client_key "$source_key"
require_value provider_credential "$provider_key"
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

raw_marker="$state_dir/raw-bucket.sha256"
fingerprint="$(printf '%s\n%s\n%s' "$CLOUDFLARE_R2_ACCOUNT_ID" "$CLOUDFLARE_R2_RAW_BUCKET" "$provider" | sha256sum | cut -d ' ' -f1)"
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

if docker container inspect "$candidate" >/dev/null 2>&1; then
  if docker container inspect "$stable" >/dev/null 2>&1 || docker container inspect "$previous" >/dev/null 2>&1; then
    docker rm -f "$candidate"
  else
    docker rename "$candidate" "$stable"
  fi
fi
if docker container inspect "$previous" >/dev/null 2>&1; then
  if docker container inspect "$stable" >/dev/null 2>&1; then
    docker rm -f "$previous"
  else
    docker rename "$previous" "$stable"
  fi
fi

docker image prune -af
docker pull "$image"
cache_mount=()
if [ "$cache_dir" != "-" ]; then
  cache_mount=(--mount "type=bind,src=$cache_dir,dst=/home/fetcher")
fi

docker run --rm \
  --name "$preflight_name" \
  --user 10001:10001 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --mount "type=bind,src=$state_dir,dst=$state_dir" \
  "${cache_mount[@]}" \
  "${runtime_env_args[@]}" \
  "$image" "$@" --check

recover() {
  status="${1:-$?}"
  [ "$status" -ne 0 ] || status=1
  set +e
  trap - ERR INT TERM HUP
  recovery_failed=0
  docker container inspect "$candidate" >/dev/null 2>&1 && docker rm -f "$candidate" >/dev/null 2>&1 || true
  if docker container inspect "$previous" >/dev/null 2>&1; then
    if docker container inspect "$stable" >/dev/null 2>&1; then
      docker rm -f "$stable" >/dev/null 2>&1 || recovery_failed=1
    fi
    if [ "$recovery_failed" -eq 0 ]; then
      docker rename "$previous" "$stable" >/dev/null 2>&1 || recovery_failed=1
    fi
  fi
  if [ "$recovery_failed" -eq 0 ] && docker container inspect "$stable" >/dev/null 2>&1; then
    docker start "$stable" >/dev/null 2>&1 || recovery_failed=1
  fi
  [ "$recovery_failed" -eq 0 ] || echo "release_fetcher_provider=failed reason=recovery_failed" >&2
  exit "$status"
}
trap 'recover "$?"' ERR
trap 'recover 130' INT
trap 'recover 143' TERM
trap 'recover 129' HUP

had_previous=0
if docker container inspect "$stable" >/dev/null 2>&1; then
  had_previous=1
  docker stop --time 30 "$stable" >/dev/null
  stable_exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$stable")"
  if [ "$stable_exit_code" -ne 0 ]; then
    echo "release_fetcher_provider=failed reason=graceful_stop" >&2
    false
  fi
  docker rename "$stable" "$previous"
fi

common_args=(
  --user 10001:10001
  --restart unless-stopped
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges
  --log-opt max-size=10m
  --log-opt max-file=3
  --tmpfs /tmp:rw,noexec,nosuid,size=16m
  --mount "type=bind,src=$state_dir,dst=$state_dir"
)
if [ "$cache_dir" != "-" ]; then
  common_args+=(--mount "type=bind,src=$cache_dir,dst=/home/fetcher")
fi
docker run -d --name "$candidate" "${common_args[@]}" "${runtime_env_args[@]}" "$image" "$@" --run-forever >/dev/null
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
[ "$had_previous" -eq 1 ] && docker rm "$previous" >/dev/null 2>&1 || true
trap - ERR INT TERM HUP
echo "release_fetcher_provider=ready provider=$provider"
