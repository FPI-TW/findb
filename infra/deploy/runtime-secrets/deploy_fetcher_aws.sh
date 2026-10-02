#!/usr/bin/env bash
# Transactional Fetcher staging deployment. Secrets are loaded independently
# for each provider by the instance role and never cross this host boundary.

set -Eeuo pipefail
set +x

: "${AWS_REGION:?AWS_REGION is required}"
: "${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID is required}"
: "${DEPLOYMENT_TARGET:?DEPLOYMENT_TARGET is required}"
: "${ECR_REGISTRY:?ECR_REGISTRY is required}"
: "${FETCHER_RELEASE_ROOT:?FETCHER_RELEASE_ROOT is required}"
: "${FETCHER_DEPLOY_MODE:?FETCHER_DEPLOY_MODE is required}"
: "${TWELVE_IMAGE_REF:?TWELVE_IMAGE_REF is required}"
: "${FINLAB_IMAGE_REF:?FINLAB_IMAGE_REF is required}"
: "${SHIOAJI_IMAGE_REF:?SHIOAJI_IMAGE_REF is required}"

FETCHER_RUNTIME_PROFILE="${FETCHER_RUNTIME_PROFILE:-bounded}"
export FETCHER_RUNTIME_PROFILE
if [[ ! "$FETCHER_RUNTIME_PROFILE" =~ ^(bounded|full-market)$ ]] \
  || { [ "$FETCHER_RUNTIME_PROFILE" = full-market ] && [ "$DEPLOYMENT_TARGET" != production ]; }; then
  echo "fetcher_aws_deploy=failed reason=runtime_profile_invalid" >&2
  exit 1
fi

expected_registry="$AWS_ACCOUNT_ID.dkr.ecr.ap-southeast-1.amazonaws.com"
if [ "$AWS_REGION" != ap-southeast-1 ] || ! [[ "$AWS_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || [[ ! "$DEPLOYMENT_TARGET" =~ ^(staging|production)$ ]] || [ "$ECR_REGISTRY" != "$expected_registry" ]; then
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
validate_image "$TWELVE_IMAGE_REF" "findb/$DEPLOYMENT_TARGET/fetcher/twelve-data"
validate_image "$FINLAB_IMAGE_REF" "findb/$DEPLOYMENT_TARGET/fetcher/finlab"
validate_image "$SHIOAJI_IMAGE_REF" "findb/$DEPLOYMENT_TARGET/fetcher/shioaji"

# Only non-secret deployment configuration is preserved. The wrapper loads one
# consumer's allowlisted values into /run and removes them after the child exits.
preserve_env=FETCHER_RUNTIME_PROFILE,AWS_REGION,AWS_ACCOUNT_ID,DEPLOYMENT_TARGET,ECR_REGISTRY,FETCHER_RELEASE_ROOT,FETCHER_DEPLOY_MODE,FETCHER_PROVIDER_RELEASE_MODE,FETCHER_SOURCE_API_URL,FINDB_SERVE_BASE_URL,FETCHER_CALENDAR_TIMEOUT_SECONDS,FETCHER_CALENDAR_CACHE_TTL_SECONDS,CLOUDFLARE_R2_ACCOUNT_ID,CLOUDFLARE_R2_RAW_BUCKET,CLOUDFLARE_R2_MAX_OBJECT_BYTES,FETCHER_REQUEST_TIMEOUT_SECONDS,FETCHER_SCHEDULER_CONTROL_POLL_SECONDS,FETCHER_MAX_ATTEMPTS,FETCHER_MAX_RETRY_AFTER_SECONDS,TWELVE_DATA_BASE_URL,TWELVE_DATA_TIMEOUT_SECONDS,TWELVE_DATA_MAX_RESPONSE_BYTES,TAIFEX_BASE_URL,TAIFEX_TIMEOUT_SECONDS,TAIFEX_MAX_RESPONSE_BYTES,SHIOAJI_SIMULATION,TWELVE_IMAGE_REF,FINLAB_IMAGE_REF,SHIOAJI_IMAGE_REF
export FETCHER_PROVIDER_RELEASE_MODE=transactional
schedule_file=/app/configs/daily_scheduler.v2.json
shioaji_manifest=/app/configs/shioaji_tw_pilot.v1.json
if [ "$DEPLOYMENT_TARGET" = production ]; then
  schedule_file=/app/configs/daily_scheduler.production.v3.json
  shioaji_manifest=/app/configs/shioaji_tw50_2026_09_21.v2.json
fi

run_runtime() {
  sudo --preserve-env="$preserve_env" "$runtime_command" \
    --catalog "$catalog" --region "$AWS_REGION" --deployment-target "$DEPLOYMENT_TARGET" --aws-account-id "$AWS_ACCOUNT_ID" "$@"
}

processed=()
rollback_failed=0
pointer_committed=0
previous_pointer=""
pointer_temp=""
rollback_provider() {
  local stable="$1" previous="$2" old_available="$3" original_id="$4" state
  # A failed retirement may leave both the original stable and a stale
  # previous container. Never replace that original with the stale copy.
  if [ "$old_available" -eq 1 ] && docker container inspect "$stable" >/dev/null 2>&1 \
    && [ "$(docker inspect --format '{{.Id}}' "$stable")" = "$original_id" ]; then
    if [ "$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$stable")" = false ]; then
      echo "fetcher_aws_deploy=failed reason=rollback_unaccepted_original runtime=$stable" >&2
      return 1
    fi
    docker start "$stable" >/dev/null || return 1
    return 0
  fi
  if [ "$old_available" -eq 1 ]; then
    if ! docker container inspect "$previous" >/dev/null 2>&1 \
      || [ "$(docker inspect --format '{{.Id}}' "$previous")" != "$original_id" ]; then
      echo "fetcher_aws_deploy=failed reason=rollback_original_missing runtime=$stable" >&2
      return 1
    fi
    if [ "$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$previous")" = false ]; then
      echo "fetcher_aws_deploy=failed reason=rollback_unaccepted_original runtime=$previous" >&2
      return 1
    fi
  fi
  if docker container inspect "$stable" >/dev/null 2>&1; then
    if docker container inspect "$previous" >/dev/null 2>&1 || [ "$old_available" -eq 0 ]; then
      state="$(docker inspect --format '{{.State.Running}}' "$stable")"
      if [ "$state" = true ]; then
        docker stop --time 30 "$stable" >/dev/null || return 1
      fi
      require_retired_runtime "$stable" || return 1
      docker rm "$stable" >/dev/null || return 1
    fi
  fi
  if [ "$old_available" -eq 1 ] && docker container inspect "$previous" >/dev/null 2>&1; then
    docker rename "$previous" "$stable" >/dev/null || return 1
  fi
  if [ "$old_available" -eq 1 ] && docker container inspect "$stable" >/dev/null 2>&1; then
    docker start "$stable" >/dev/null || return 1
  fi
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
  if [ "$rollback_failed" -ne 0 ]; then
    echo "fetcher_aws_deploy=failed reason=transaction_rollback_failed" >&2
    return 1
  fi
}
abort_transaction() {
  local status="${1:-$?}"
  trap - ERR INT TERM HUP
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

register_provider() {
  local stable="$1" previous="$2" old_available=0 stable_accepted original_id=- unsafe_previous=0
  if docker container inspect "$stable" >/dev/null 2>&1; then
    stable_accepted="$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$stable")"
    if [ "$stable_accepted" != false ]; then
      old_available=1
      original_id="$(docker inspect --format '{{.Id}}' "$stable")"
    fi
  fi
  if [ "$old_available" -eq 0 ] && docker container inspect "$previous" >/dev/null 2>&1; then
    if [ "$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$previous")" = false ]; then
      unsafe_previous=1
    else
      old_available=1
      original_id="$(docker inspect --format '{{.Id}}' "$previous")"
    fi
  fi
  # One complete append is the registration boundary. A signal can never see
  # a partially registered provider, and no runtime mutation precedes it.
  processed+=("${stable}:${previous}:${old_available}:${original_id}")
  if [ "$unsafe_previous" -eq 1 ]; then
    echo "fetcher_aws_deploy=failed reason=unaccepted_retained_runtime runtime=$previous" >&2
    return 1
  fi
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
  local stable="$1" provider repository digest
  [ "$DEPLOYMENT_TARGET" = staging ] && [ "$FETCHER_RUNTIME_PROFILE" = bounded ] || return 1
  case "$stable" in
    findb-fetcher-scheduler-historical)
      provider=twelve_data; repository=twelve-data; digest=2f64ab8c40e082e6601a00839b2345c35c504afcf1d453026d779ac151f46d03 ;;
    findb-fetcher-finlab-scheduler-historical)
      provider=finlab; repository=finlab; digest=bcd882dff3412a55f14921474d16ca27950dc51f37252fae404b03eccb862bc2 ;;
    findb-fetcher-shioaji-scheduler-historical)
      provider=shioaji; repository=shioaji; digest=161991e5b056becbdc35d67c547a7092807f1e0aa66a34b7b921ed57ecc6bd13 ;;
    *) return 1 ;;
  esac
  # Transitional exception for the observed pre-signal-handler staging binaries only.
  # They ignore SIGTERM as PID 1. Leased dates replay after server expiry;
  # this is crash recovery, never a successful graceful shutdown.
  [ "$AWS_ACCOUNT_ID" = 439622209937 ] \
    && [ "$(docker inspect --format '{{.Config.Image}}' "$stable")" = "$expected_registry/findb/staging/fetcher/$repository@sha256:$digest" ] \
    && [ "$(docker inspect --format '{{.Path}}' "$stable")" = findb-fetch-historical-backfill ] \
    && [ "$(docker inspect --format '{{json .Args}}' "$stable")" = "[\"--provider\",\"$provider\",\"--run-forever\"]" ] \
    && [ "$(docker inspect --format '{{ join .Config.Cmd " " }}' "$stable")" = "findb-fetch-historical-backfill --provider $provider --run-forever" ] \
    && [ "$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$stable")" = true ] \
    && [ -z "$(docker inspect --format '{{range $key, $_ := .Config.Labels}}{{if eq $key "com.findb.fetcher.historical-shutdown"}}present{{end}}{{end}}' "$stable")" ]
}

require_retired_runtime() {
  local stable="$1" running exit_code oom error
  running="$(docker inspect --format '{{.State.Running}}' "$stable")"
  exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$stable")"
  oom="$(docker inspect --format '{{.State.OOMKilled}}' "$stable")"
  error="$(docker inspect --format '{{.State.Error}}' "$stable")"
  if [ "$running" = false ] && [ "$oom" = false ] && [ -z "$error" ]; then
    if [ "$exit_code" = 0 ]; then
      return 0
    fi
    if [ "$exit_code" = 137 ] && legacy_historical_shutdown "$stable"; then
      echo "fetcher_aws_deploy=legacy_historical_crash_recovery runtime=$stable lease_policy=expire_and_replay" >&2
      return 0
    fi
  fi
  # Docker's Error may include host details. Emit only a fixed safe reason.
  echo "fetcher_aws_deploy=failed reason=runtime_retirement_failed runtime=$stable" >&2
  return 1
}

retire_runtime() {
  local stable="$1" previous="${1}-previous"
  register_provider "$stable" "$previous"
  if docker container inspect "$stable" >/dev/null 2>&1; then
    if [ "$(docker inspect --format '{{.State.Running}}' "$stable")" = true ]; then
      if ! docker stop --time 30 "$stable" >/dev/null 2>&1; then
        echo "fetcher_aws_deploy=failed reason=runtime_stop_failed runtime=$stable" >&2
        return 1
      fi
    fi
    require_retired_runtime "$stable"
    # Interrupted unaccepted stable state must not displace the accepted
    # original selected from previous. Apply the same strict stop gate first.
    if [ "$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$stable")" = false ]; then
      docker rm "$stable" >/dev/null
      return 0
    fi
    if docker container inspect "$previous" >/dev/null 2>&1; then
      docker rm "$previous" >/dev/null
    fi
    docker rename "$stable" "$previous"
  fi
}

# Preserve historical workers for transaction recovery. Only bounded creates
# replacement workers; expanded runtime never consumes historical requests.
for stable in findb-fetcher-scheduler findb-fetcher-finlab-scheduler findb-fetcher-shioaji-scheduler; do
  retire_runtime "${stable}-historical"
done
provider_count=3
if [ "$FETCHER_RUNTIME_PROFILE" = full-market ]; then
  full_market_state=/var/lib/findb-full-market
  for provider in twelve-data finlab shioaji taifex; do
    case "$provider" in
      twelve-data) identity=twelve_data; image="$TWELVE_IMAGE_REF"; stable=findb-fetcher-scheduler ;;
      finlab) identity=finlab; image="$FINLAB_IMAGE_REF"; stable=findb-fetcher-finlab-scheduler ;;
      shioaji) identity=shioaji; image="$SHIOAJI_IMAGE_REF"; stable=findb-fetcher-shioaji-scheduler ;;
      taifex) identity=taifex; image="$TWELVE_IMAGE_REF"; stable=findb-fetcher-taifex-scheduler ;;
    esac
    cache_dir=-
    case "$provider" in
      finlab) cache_dir=/var/lib/findb-finlab-fetcher/cache ;;
      shioaji) cache_dir=/var/lib/findb-shioaji-fetcher/cache ;;
    esac
    register_provider "$stable" "${stable}-previous"
    run_runtime --consumer "$provider" --ecr-registry "$ECR_REGISTRY" --docker-login -- \
      "$provider_helper" "$provider" "$image" \
      "$full_market_state" "$full_market_state/state.sqlite3" \
      "$stable" "${stable}-candidate" "${stable}-previous" "${stable}-preflight" "$cache_dir" \
      findb-fetch-full-market --config /app/configs/full_market.production.v1.json \
      --provider "$identity" --readiness-file "$full_market_state/readiness/$identity.json"
  done
  provider_count=4
else
  # Switching back to bounded stops the TAIFEX process without deleting state.
  retire_runtime findb-fetcher-taifex-scheduler
  register_provider findb-fetcher-scheduler findb-fetcher-scheduler-previous
  run_runtime --consumer twelve-data --ecr-registry "$ECR_REGISTRY" --docker-login -- \
    "$provider_helper" twelve-data "$TWELVE_IMAGE_REF" \
    /var/lib/findb-fetcher /var/lib/findb-fetcher/state.sqlite3 \
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
    /var/lib/findb-shioaji-fetcher /var/lib/findb-shioaji-fetcher/state.sqlite3 \
    findb-fetcher-shioaji-scheduler findb-fetcher-shioaji-scheduler-candidate findb-fetcher-shioaji-scheduler-previous \
    findb-fetcher-shioaji-scheduler-preflight /var/lib/findb-shioaji-fetcher/cache \
    findb-fetch-shioaji-scheduler --manifest "$shioaji_manifest"
fi

if [ "$FETCHER_DEPLOY_MODE" = candidate ]; then
  rollback_processed
  trap - ERR INT TERM HUP
  echo "fetcher_aws_deploy=candidate_ready_for_acceptance providers=$provider_count profile=$FETCHER_RUNTIME_PROFILE"
  exit 0
fi

trap - ERR INT TERM HUP
for entry in "${processed[@]}"; do
  remainder="${entry#*:}"
  previous="${remainder%%:*}"
  if docker container inspect "$previous" >/dev/null 2>&1; then
    docker rm "$previous" >/dev/null
  fi
done
echo "fetcher_aws_deploy=activated providers=$provider_count profile=$FETCHER_RUNTIME_PROFILE"
