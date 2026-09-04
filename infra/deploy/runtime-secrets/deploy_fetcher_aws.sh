#!/usr/bin/env bash
# Transactional Fetcher staging deployment. Secrets are loaded independently
# for each provider by the instance role and never cross this host boundary.

set -euo pipefail
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
preserve_env=AWS_REGION,AWS_ACCOUNT_ID,DEPLOYMENT_TARGET,ECR_REGISTRY,FETCHER_RELEASE_ROOT,FETCHER_DEPLOY_MODE,FETCHER_PROVIDER_RELEASE_MODE,FETCHER_SOURCE_API_URL,FINDB_SERVE_BASE_URL,FETCHER_CALENDAR_TIMEOUT_SECONDS,FETCHER_CALENDAR_CACHE_TTL_SECONDS,CLOUDFLARE_R2_ACCOUNT_ID,CLOUDFLARE_R2_RAW_BUCKET,CLOUDFLARE_R2_MAX_OBJECT_BYTES,FETCHER_REQUEST_TIMEOUT_SECONDS,FETCHER_SCHEDULER_CONTROL_POLL_SECONDS,FETCHER_MAX_ATTEMPTS,FETCHER_MAX_RETRY_AFTER_SECONDS,TWELVE_DATA_BASE_URL,TWELVE_DATA_TIMEOUT_SECONDS,TWELVE_DATA_MAX_RESPONSE_BYTES,SHIOAJI_SIMULATION,TWELVE_IMAGE_REF,FINLAB_IMAGE_REF,SHIOAJI_IMAGE_REF
export FETCHER_PROVIDER_RELEASE_MODE=transactional

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
  local stable="$1" previous="$2" old_available="$3" state
  if docker container inspect "$stable" >/dev/null 2>&1; then
    if docker container inspect "$previous" >/dev/null 2>&1 || [ "$old_available" -eq 0 ]; then
      state="$(docker inspect --format '{{.State.Running}}' "$stable")"
      if [ "$state" = true ]; then
        docker stop --time 30 "$stable" >/dev/null || return 1
        [ "$(docker inspect --format '{{.State.ExitCode}}' "$stable")" -eq 0 ] || return 1
      fi
      docker rm "$stable" >/dev/null || return 1
    fi
  fi
  if docker container inspect "$previous" >/dev/null 2>&1; then
    docker rename "$previous" "$stable" >/dev/null || return 1
  fi
  if [ "$old_available" -eq 1 ] && docker container inspect "$stable" >/dev/null 2>&1; then
    docker start "$stable" >/dev/null || return 1
  fi
}
rollback_processed() {
  local index entry stable remainder previous old_available
  for ((index=${#processed[@]}-1; index>=0; index--)); do
    entry="${processed[$index]}"
    stable="${entry%%:*}"
    remainder="${entry#*:}"
    previous="${remainder%%:*}"
    old_available="${entry##*:}"
    rollback_provider "$stable" "$previous" "$old_available" || rollback_failed=1
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
  local stable="$1" previous="$2" old_available=0 stable_accepted
  if docker container inspect "$previous" >/dev/null 2>&1; then
    old_available=1
  elif docker container inspect "$stable" >/dev/null 2>&1; then
    stable_accepted="$(docker inspect --format '{{ index .Config.Labels "com.findb.fetcher.accepted" }}' "$stable")"
    [ "$stable_accepted" = false ] || old_available=1
  fi
  processed+=("${stable}:${previous}:${old_available}")
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

register_provider findb-fetcher-scheduler findb-fetcher-scheduler-previous
run_runtime --consumer twelve-data --ecr-registry "$ECR_REGISTRY" --docker-login -- \
  "$provider_helper" twelve-data "$TWELVE_IMAGE_REF" \
  /var/lib/findb-fetcher /var/lib/findb-fetcher/state.sqlite3 \
  findb-fetcher-scheduler findb-fetcher-scheduler-candidate findb-fetcher-scheduler-previous \
  findb-fetcher-scheduler-preflight - \
  findb-fetch-scheduler --schedule-file /app/configs/daily_scheduler.v2.json \
  --slot-id western_markets_window --dataset-key us_equity_eod

register_provider findb-fetcher-finlab-scheduler findb-fetcher-finlab-scheduler-previous
run_runtime --consumer finlab --ecr-registry "$ECR_REGISTRY" --docker-login -- \
  "$provider_helper" finlab "$FINLAB_IMAGE_REF" \
  /var/lib/findb-finlab-fetcher /var/lib/findb-finlab-fetcher/state.sqlite3 \
  findb-fetcher-finlab-scheduler findb-fetcher-finlab-scheduler-candidate findb-fetcher-finlab-scheduler-previous \
  findb-fetcher-finlab-scheduler-preflight /var/lib/findb-finlab-fetcher/cache \
  findb-fetch-finlab-scheduler --schedule-file /app/configs/daily_scheduler.v2.json \
  --slot-id taiwan_market_window --dataset-key tw_equity_eod

register_provider findb-fetcher-shioaji-scheduler findb-fetcher-shioaji-scheduler-previous
run_runtime --consumer shioaji --ecr-registry "$ECR_REGISTRY" --docker-login -- \
  "$provider_helper" shioaji "$SHIOAJI_IMAGE_REF" \
  /var/lib/findb-shioaji-fetcher /var/lib/findb-shioaji-fetcher/state.sqlite3 \
  findb-fetcher-shioaji-scheduler findb-fetcher-shioaji-scheduler-candidate findb-fetcher-shioaji-scheduler-previous \
  findb-fetcher-shioaji-scheduler-preflight /var/lib/findb-shioaji-fetcher/cache \
  findb-fetch-shioaji-scheduler --manifest /app/configs/shioaji_tw_pilot.v1.json

if [ "$FETCHER_DEPLOY_MODE" = candidate ]; then
  rollback_processed
  trap - ERR INT TERM HUP
  echo "fetcher_aws_deploy=candidate_ready_for_acceptance providers=3"
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
echo "fetcher_aws_deploy=activated providers=3"
