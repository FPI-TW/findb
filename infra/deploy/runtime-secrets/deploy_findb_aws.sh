#!/usr/bin/env bash
# FinDB staging AWS-mode deployment. Runtime secrets are loaded on the host
# from the instance role and are never supplied by the runner.

set -euo pipefail
set +x

if [ -n "${FINDB_RELEASE_ROOT:-}" ]; then
  release_root="$FINDB_RELEASE_ROOT"
  runtime_dir="$release_root/infra/deploy/runtime-secrets"
  compose_file="$release_root/docker-compose.prod.yml"
  # A release-root Compose file must still manage the stable FinDB project,
  # whose containers have fixed names and are validated below by project label.
  COMPOSE_PROJECT_NAME=findb
  export COMPOSE_PROJECT_NAME
else
  # Explicit production compatibility: staging always supplies a validated
  # release root, while legacy production retains its established layout.
  release_root=/opt/findb
  runtime_dir=/opt/findb/runtime-secrets
  compose_file=/opt/findb/docker-compose.prod.yml
fi
catalog="$runtime_dir/findb.json"
runtime_command="$runtime_dir/runtime_secret_command.sh"
nginx_runtime="$runtime_dir/render_nginx_runtime.sh"
# Production retains its established host path.  A validated staging release
# must opt into the root-owned path below; it must never render as root below
# an SSH user's home directory.
nginx_config_dir="${FINDB_NGINX_CONFIG_DIR:-/home/ubuntu/etc/nginx}"

: "${AWS_REGION:?AWS_REGION is required}"
: "${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID is required}"
: "${DEPLOYMENT_TARGET:?DEPLOYMENT_TARGET is required}"

if [ "$AWS_REGION" != "ap-southeast-1" ] || ! [[ "$AWS_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || [[ ! "$DEPLOYMENT_TARGET" =~ ^(staging|production)$ ]]; then
  echo "findb_aws_deploy=failed reason=region_invalid" >&2
  exit 1
fi

: "${DASHBOARD_IMAGE_REF:?DASHBOARD_IMAGE_REF is required}"
: "${FINDB_PUBLIC_HOST:?FINDB_PUBLIC_HOST is required}"
: "${ECR_REGISTRY:?ECR_REGISTRY is required}"
: "${FINDB_IMAGE_REF:?FINDB_IMAGE_REF is required}"

expected_ecr_registry="$AWS_ACCOUNT_ID.dkr.ecr.ap-southeast-1.amazonaws.com"
expected_findb_image="$expected_ecr_registry/findb/$DEPLOYMENT_TARGET/backend@sha256:"
expected_dashboard_image="$expected_ecr_registry/findb/$DEPLOYMENT_TARGET/dashboard@sha256:"

if [ "$ECR_REGISTRY" != "$expected_ecr_registry" ] \
  || ! printf '%s' "$FINDB_IMAGE_REF" | grep -Eq "^${expected_findb_image}[0-9a-f]{64}$" \
  || ! printf '%s' "$DASHBOARD_IMAGE_REF" | grep -Eq "^${expected_dashboard_image}[0-9a-f]{64}$"; then
  echo "findb_aws_deploy=failed reason=ecr_image_contract" >&2
  exit 1
fi
if [ -n "${FINDB_RELEASE_ROOT:-}" ]; then
  deploy_mode="${FINDB_DEPLOY_MODE:-}"
  case "$deploy_mode" in
    candidate|activate) ;;
    *) echo "findb_aws_deploy=failed reason=staging_deploy_mode_invalid" >&2; exit 1 ;;
  esac
else
  # Explicit production compatibility: production continues to use its legacy
  # single-command behaviour and never enters the staging transaction modes.
  deploy_mode=legacy
fi
# Only nonsecret deployment settings are preserved across sudo. The runtime
# command itself creates and loads the secret environment after this boundary.
preserve_env=AWS_REGION,AWS_ACCOUNT_ID,DEPLOYMENT_TARGET,ECR_REGISTRY,FINDB_IMAGE_REF,DASHBOARD_IMAGE_REF,FINDB_PUBLIC_HOST,FINDB_NGINX_CONFIG_DIR,COMPOSE_FILE,APP_NAME,APP_VERSION,DEBUG,PORT,DATABASE_POOL_SIZE,DATABASE_MAX_OVERFLOW,API_V1_PREFIX,API_KEY_HEADER,SOURCE_ALLOWLIST_CIDRS,SOURCE_TRUST_PROXY_HEADERS,SERVE_REQUIRE_AUTH,RATE_LIMIT_REQUESTS,RATE_LIMIT_WINDOW,RAW_RETENTION_ENABLED,RAW_RETENTION_DAYS,FINDB_STATIC_CACHE_BASE_URL,FINDB_LATEST_PRICE_WORKERS,CLOUDFLARE_R2_ACCOUNT_ID,CLOUDFLARE_R2_CANONICAL_BUCKET
if [ -n "${FINDB_RELEASE_ROOT:-}" ]; then
  preserve_env="${preserve_env},COMPOSE_PROJECT_NAME,FINDB_RELEASE_ROOT,FINDB_DEPLOY_MODE,PREDEPLOY_EXPECTED_ALEMBIC_REVISION,PREDEPLOY_EXPECTED_RDS_ENDPOINT"
fi

run_runtime() {
  sudo --preserve-env="$preserve_env" "$runtime_command" \
    --catalog "$catalog" \
    --region "$AWS_REGION" \
    --deployment-target "$DEPLOYMENT_TARGET" \
    --aws-account-id "$AWS_ACCOUNT_ID" \
    "$@"
}

if [ ! -f "$compose_file" ]; then
  echo "findb_aws_deploy=failed reason=compose_missing" >&2
  exit 1
fi

release_services_may_have_started=0
transaction_finalized=0
fixed_candidate_containers=(
  findb-nginx findb-dashboard findb-serve findb-ingest findb-dispatcher
  findb-worker findb-raw-cleanup
)
stop_fixed_candidate_containers() {
  local names container state cleanup_failed=0
  if ! names="$(docker ps -a --format '{{.Names}}')"; then
    echo "findb_aws_deploy=failed reason=candidate_cleanup_docker_list_failed" >&2
    return 1
  fi
  for container in "${fixed_candidate_containers[@]}"; do
    # A container that has not been created cannot be active; Docker itself
    # was successfully queried above, so this is distinct from an inspection
    # failure that must fail the transaction.
    if ! printf '%s\n' "$names" | grep -Fxq "$container"; then
      continue
    fi
    if ! state="$(docker inspect --format '{{.State.Status}} {{.State.Running}} {{.State.Restarting}}' "$container")"; then
      echo "findb_aws_deploy=failed reason=candidate_cleanup_docker_inspect_failed container=$container" >&2
      cleanup_failed=1
      continue
    fi
    case "$state" in
      created\ false\ false|exited\ false\ false|dead\ false\ false) ;;
      *)
        if ! docker stop --time 30 "$container" >/dev/null; then
          echo "findb_aws_deploy=failed reason=candidate_cleanup_docker_stop_failed container=$container" >&2
          cleanup_failed=1
          continue
        fi
        if ! state="$(docker inspect --format '{{.State.Status}} {{.State.Running}} {{.State.Restarting}}' "$container")"; then
          echo "findb_aws_deploy=failed reason=candidate_cleanup_docker_verify_failed container=$container" >&2
          cleanup_failed=1
          continue
        fi
        case "$state" in
          created\ false\ false|exited\ false\ false|dead\ false\ false) ;;
          *)
            echo "findb_aws_deploy=failed reason=candidate_cleanup_container_still_active container=$container" >&2
            cleanup_failed=1
            ;;
        esac
        ;;
    esac
  done
  if [ "$cleanup_failed" -ne 0 ]; then
    echo "findb_aws_deploy=failed reason=candidate_cleanup_failed" >&2
    return 1
  fi
  # RabbitMQ and its durable volume are deliberately not part of this fixed
  # application/public/writer cleanup set.
  echo "findb_aws_deploy=candidate_services_fail_stopped"
}
cleanup_unaccepted_candidate() {
  status=$?
  trap - EXIT
  if [ -n "${FINDB_RELEASE_ROOT:-}" ] \
    && [ "$release_services_may_have_started" -eq 1 ] \
    && [ "$transaction_finalized" -ne 1 ]; then
    if ! stop_fixed_candidate_containers; then
      # A cleanup failure is never hidden behind the original command error.
      exit 1
    fi
    echo "findb_aws_deploy=failed reason=unaccepted_candidate_stopped" >&2
  fi
  exit "$status"
}
trap cleanup_unaccepted_candidate EXIT

require_root_owned_nginx_directory() {
  local directory="$1"
  [ -d "$directory" ] && [ ! -L "$directory" ] \
    && [ "$(stat -c '%u:%g:%a' "$directory")" = "0:0:755" ] || {
      echo "findb_aws_deploy=failed reason=nginx_config_directory_unsafe" >&2
      exit 1
    }
}

if [ -n "${FINDB_RELEASE_ROOT:-}" ]; then
  if [ "$deploy_mode" = candidate ]; then
    nginx_config_dir="$release_root/rendered-nginx"
    install -d -o root -g root -m 0755 "$nginx_config_dir"
    export FINDB_NGINX_CONFIG_DIR="$nginx_config_dir"
  elif [ "$nginx_config_dir" != /etc/findb/nginx ]; then
    echo "findb_aws_deploy=failed reason=staging_nginx_config_path_invalid" >&2
    exit 1
  fi
  if [ "$deploy_mode" != candidate ]; then
    require_root_owned_nginx_directory /etc
    require_root_owned_nginx_directory /etc/findb
  fi
  require_root_owned_nginx_directory "$nginx_config_dir"
  for conf in nginx.conf source-allowlist.conf cloudflare-real-ip.conf; do
    if [ -e "$nginx_config_dir/$conf" ] \
      && { [ -L "$nginx_config_dir/$conf" ] || [ ! -f "$nginx_config_dir/$conf" ]; }; then
      echo "findb_aws_deploy=failed reason=nginx_config_target_unsafe" >&2
      exit 1
    fi
  done
  render_nginx_files() {
    python3 "$release_root/backend/scripts/render_nginx_public_host.py" \
      --host "$FINDB_PUBLIC_HOST" --template "$release_root/infra/nginx/nginx.conf" \
      --output "$nginx_config_dir/nginx.conf"
    python3 "$release_root/backend/scripts/render_nginx_source_allowlist.py" \
      --cidrs "$SOURCE_ALLOWLIST_CIDRS" --output "$nginx_config_dir/source-allowlist.conf"
    python3 "$release_root/backend/scripts/render_nginx_cloudflare_real_ip.py" \
      --output "$nginx_config_dir/cloudflare-real-ip.conf"
  }
  if [ "$DEPLOYMENT_TARGET" = production ]; then
    run_runtime --consumer deployment -- bash -s -- \
      "$release_root" "$nginx_config_dir" "$FINDB_PUBLIC_HOST" <<'RENDER_NGINX_SCRIPT'
set -euo pipefail
release_root="$1"
nginx_config_dir="$2"
public_host="$3"
python3 "$release_root/backend/scripts/render_nginx_public_host.py" \
  --host "$public_host" --template "$release_root/infra/nginx/nginx.conf" \
  --output "$nginx_config_dir/nginx.conf"
python3 "$release_root/backend/scripts/render_nginx_source_allowlist.py" \
  --cidrs "$SOURCE_ALLOWLIST_CIDRS" --output "$nginx_config_dir/source-allowlist.conf"
python3 "$release_root/backend/scripts/render_nginx_cloudflare_real_ip.py" \
  --output "$nginx_config_dir/cloudflare-real-ip.conf"
RENDER_NGINX_SCRIPT
  else
    render_nginx_files
  fi
fi
for conf in nginx.conf source-allowlist.conf cloudflare-real-ip.conf; do
  if [ ! -f "$nginx_config_dir/$conf" ] || [ -L "$nginx_config_dir/$conf" ]; then
    echo "findb_aws_deploy=failed reason=nginx_config_missing_or_unsafe" >&2
    exit 1
  fi
  if [ -n "${FINDB_RELEASE_ROOT:-}" ]; then
    if ! chown root:root -- "$nginx_config_dir/$conf" \
      || ! chmod 0644 -- "$nginx_config_dir/$conf"; then
      echo "findb_aws_deploy=failed reason=nginx_config_metadata_update_failed" >&2
      exit 1
    fi
    if [ "$(stat -c '%u:%g:%a' "$nginx_config_dir/$conf")" != "0:0:644" ]; then
      echo "findb_aws_deploy=failed reason=nginx_config_metadata_invalid" >&2
      exit 1
    fi
  fi
done
for tls_file in server.crt server.key; do
  if [ "$DEPLOYMENT_TARGET" = staging ] \
    && [ ! -f "/home/ubuntu/etc/nginx/ssl/$tls_file" ]; then
    echo "findb_aws_deploy=failed reason=tls_file_missing" >&2
    exit 1
  fi
done

# Compose interpolation needs the compose consumer while pulling images. The
# registry token is confined to the same tmpfs Docker config and command.
run_runtime --consumer compose --ecr-registry "$ECR_REGISTRY" --docker-login -- bash -s -- "$compose_file" <<'PULL_SCRIPT'
set -euo pipefail
compose_file="$1"
docker compose -f "$compose_file" pull
PULL_SCRIPT

# A candidate must validate that its target-scoped nginx secret can load, but
# it must never replace the live nginx bind mount under /run.  Its rendered
# non-secret nginx configuration is already confined to $release_root above;
# the runtime wrapper loads the nginx catalog into a short-lived tmpfs env and
# removes it before returning.  Only activation/legacy may render the live
# lookup-key file consumed by the fixed nginx container.
if [ "$deploy_mode" = candidate ]; then
  # Validate target-scoped TLS in short-lived tmpfs files without replacing
  # any live nginx bind mount before durable candidate acceptance.
  sudo --preserve-env="$preserve_env" "$nginx_runtime" "$catalog" "$AWS_REGION" \
    "$FINDB_PUBLIC_HOST" "$DEPLOYMENT_TARGET" "$AWS_ACCOUNT_ID" validate
  echo "findb_aws_deploy=candidate_nginx_runtime_validated"
else
  sudo --preserve-env="$preserve_env" "$nginx_runtime" "$catalog" "$AWS_REGION" "$FINDB_PUBLIC_HOST" "$DEPLOYMENT_TARGET" "$AWS_ACCOUNT_ID"
  if [ "$DEPLOYMENT_TARGET" = production ]; then
    for tls_file in server.crt server.key; do
      if [ ! -f "/run/findb-runtime-secrets/nginx/$tls_file" ] \
        || [ "$(stat -c '%u:%g:%a' "/run/findb-runtime-secrets/nginx/$tls_file")" != "0:0:600" ]; then
        echo "findb_aws_deploy=failed reason=tls_file_missing_or_unsafe" >&2
        exit 1
      fi
    done
  fi
fi

run_runtime --consumer migration --consumer compose --map MIGRATION_DATABASE_URL=DATABASE_URL -- bash -s -- "$compose_file" <<'MIGRATION_CHECK_SCRIPT'
set -euo pipefail
compose_file="$1"
bootstrap_arg=""
if [ "$DEPLOYMENT_TARGET" = production ]; then
  bootstrap_arg=--allow-empty-database-bootstrap
fi
if [ -n "${FINDB_RELEASE_ROOT:-}" ]; then
  expected_revision="${PREDEPLOY_EXPECTED_ALEMBIC_REVISION:?staging target revision is required}"
  expected_rds_endpoint="${PREDEPLOY_EXPECTED_RDS_ENDPOINT:?staging RDS endpoint is required}"
  if [ "${FINDB_DEPLOY_MODE:-candidate}" = activate ]; then
    docker compose -f "$compose_file" run --rm --no-deps ingest \
      python /app/scripts/predeploy_db_check.py \
        --expected-alembic-revision "$expected_revision" \
        --expected-rds-endpoint "$expected_rds_endpoint" \
        --require-exact-alembic-revision \
        ${bootstrap_arg:+"$bootstrap_arg"}
  else
    docker compose -f "$compose_file" run --rm --no-deps ingest \
      python /app/scripts/predeploy_db_check.py \
        --expected-alembic-revision "$expected_revision" \
        --expected-rds-endpoint "$expected_rds_endpoint" \
        ${bootstrap_arg:+"$bootstrap_arg"}
  fi
else
  docker compose -f "$compose_file" run --rm --no-deps ingest \
    python /app/scripts/predeploy_db_check.py \
      ${bootstrap_arg:+"$bootstrap_arg"}
fi
MIGRATION_CHECK_SCRIPT

# Candidate validation is deliberately non-disruptive: it has authenticated,
# pulled every exact digest, rendered/validated configuration, loaded the
# target-scoped secret catalog, and completed the read-only DB predeploy gate.
# It must return before stopping writers or creating any fixed-name Compose
# container. Durable acceptance is persisted by the orchestrator afterward;
# only the activation invocation may mutate the live Compose project.
if [ "$deploy_mode" = candidate ]; then
  transaction_finalized=1
  echo "findb_aws_deploy=candidate_ready_for_acceptance image_refs=exact-digests live_containers=untouched"
  exit 0
fi

if [ "$deploy_mode" = activate ] || [ "$deploy_mode" = legacy ]; then
writer_services=(ingest dispatcher worker raw-cleanup)
run_runtime --consumer compose -- bash -s -- "$compose_file" "${writer_services[*]}" <<'STOP_SCRIPT'
set -euo pipefail
compose_file="$1"
shift
read -r -a writer_services <<< "$1"
docker compose -f "$compose_file" stop --timeout 30 "${writer_services[@]}"

for service in "${writer_services[@]}"; do
  container_ids="$(docker compose -f "$compose_file" ps -aq "$service")"
  if [ -n "$container_ids" ]; then
    while IFS= read -r container_id; do
      [ -n "$container_id" ] || continue
      state="$(docker inspect --format '{{.State.Status}} {{.State.Running}} {{.State.Restarting}}' "$container_id")"
      case "$state" in
        exited\ false\ false|created\ false\ false) ;;
        *) echo "findb_aws_deploy=failed reason=writer_not_stopped service=$service" >&2; exit 1 ;;
      esac
    done <<< "$container_ids"
  fi
done
echo "findb_aws_deploy=writers_stopped"
STOP_SCRIPT

# Keep the migration URL confined to one-shot commands. It is mapped to the
# compose DATABASE_URL name only for this invocation and never for a long-lived
# service startup.
# Preserve the application URL under a one-shot name before mapping the
# migration URL to DATABASE_URL. The reconciliation command needs both
# identities, while neither migration credential is exposed to long-lived
# services.
run_runtime --consumer migration --consumer compose --consumer credentials \
  --map DATABASE_URL=APPLICATION_DATABASE_URL \
  --map MIGRATION_DATABASE_URL=DATABASE_URL \
  -- bash -s -- "$compose_file" <<'MIGRATION_SCRIPT'
set -euo pipefail
compose_file="$1"
docker compose -f "$compose_file" run --rm --no-deps ingest sh -euc '
  uv run alembic upgrade head
  uv run alembic current
'
docker compose -f "$compose_file" run --rm --no-deps \
  -e APPLICATION_DATABASE_URL ingest \
  python /app/scripts/reconcile_database_privileges.py
docker compose -f "$compose_file" run --rm --no-deps \
  -e APPLICATION_DATABASE_URL \
  -e FINDB_QUEUE_HEALTH_ADMIN_API_KEY \
  -e FINDB_LOOKUP_SERVE_API_KEY \
  -e FINDB_STATIC_CACHE_SERVE_API_KEY ingest \
  python /app/scripts/reconcile_deployment_credentials.py
docker compose -f "$compose_file" run --rm --no-deps ingest \
  python /app/scripts/provision_registry.py \
  --deployment-target "$DEPLOYMENT_TARGET"
if [ "$DEPLOYMENT_TARGET" = production ]; then
  docker compose -f "$compose_file" run --rm --no-deps ingest \
    python /app/scripts/seed_production_calendars.py \
    --deployment-target production \
    --apply \
    --actor production-bootstrap
fi
MIGRATION_SCRIPT
fi

# The candidate can now replace/start services. Preflight and migration errors
# intentionally leave the stable public containers alone; writer-stop remains
# fail-closed before the single normal-deployment migration.
if [ -n "${FINDB_RELEASE_ROOT:-}" ]; then
  release_services_may_have_started=1
fi
run_runtime --consumer compose -- bash -s -- "$compose_file" <<'UP_SCRIPT'
set -euo pipefail
compose_file="$1"
docker compose -f "$compose_file" up -d --remove-orphans </dev/null

if ! docker compose -f "$compose_file" exec -T rabbitmq rabbitmq-diagnostics -q ping </dev/null >/dev/null; then
  echo "findb_aws_deploy=failed reason=rabbitmq_unhealthy" >&2
  exit 1
fi
topology_ready=0
for attempt in $(seq 1 24); do
  queue_topology="$(docker compose -f "$compose_file" exec -T rabbitmq \
    rabbitmqctl list_queues -p /findb name durable --formatter json </dev/null 2>/dev/null || true)"
  if printf '%s' "$queue_topology" | python3 -c '
import json, sys
try:
    queues = json.load(sys.stdin)
except json.JSONDecodeError:
    raise SystemExit(1)
required = {"findb.normalize.v1", "findb.normalize.dlq.v1"}
actual = {item.get("name") for item in queues if item.get("durable") is True}
raise SystemExit(0 if required <= actual else 1)
'; then
    topology_ready=1
    break
  fi
  sleep 5
done
if [ "$topology_ready" -ne 1 ]; then
  echo "findb_aws_deploy=failed reason=rabbitmq_topology_invalid vhost=/findb" >&2
  exit 1
fi
for service in dispatcher worker; do
  ready=0
  for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if docker compose -f "$compose_file" ps --status running --services </dev/null | grep -qx "$service"; then
      ready=1
      break
    fi
    sleep 5
  done
  if [ "$ready" -ne 1 ]; then
    echo "findb_aws_deploy=failed reason=service_not_running service=$service" >&2
    exit 1
  fi
done

worker_ping_ready=0
for attempt in $(seq 1 24); do
  if docker compose -f "$compose_file" exec -T worker \
    celery -A app.task_queue inspect ping --timeout=10 </dev/null >/dev/null 2>&1; then
    worker_ping_ready=1
    break
  fi
  sleep 5
done
if [ "$worker_ping_ready" -ne 1 ]; then
  echo "findb_aws_deploy=failed reason=celery_worker_ping_failed" >&2
  exit 1
fi

for service in serve ingest; do
  ready=0
  for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if docker compose -f "$compose_file" exec -T "$service" \
      sh -lc 'python -c "import os, urllib.request; port=os.getenv(\"PORT\", \"8080\"); urllib.request.urlopen(f\"http://127.0.0.1:{port}/health\", timeout=5)"' \
      </dev/null >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 5
  done
  if [ "$ready" -ne 1 ]; then
    echo "findb_aws_deploy=failed reason=app_not_ready service=$service" >&2
    exit 1
  fi
done

ready=0
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if docker compose -f "$compose_file" exec -T dashboard wget -q --spider http://127.0.0.1:3333/dashboard/ </dev/null; then
    ready=1
    break
  fi
  sleep 5
done
if [ "$ready" -ne 1 ]; then
  echo "findb_aws_deploy=failed reason=dashboard_not_ready" >&2
  exit 1
fi

docker compose -f "$compose_file" exec -T -e CELERY_BROKER_URL ingest \
  python /app/scripts/check_queue_health.py \
  --attempts 12 --interval 5 --maximum-heartbeat-age 90 </dev/null >/dev/null

# The prior compose restart did not reliably load the newly rendered single-file
# tmpfs bind mount. Replace the exact, verified nginx container so Docker mounts
# the current source path; verify that replacement before health and key probes.
nginx_expected_project=findb
nginx_expected_service=nginx
nginx_serve_key_source=/run/findb-runtime-secrets/nginx/serve-key.conf
nginx_old_id="$(docker compose -f "$compose_file" ps -q nginx </dev/null)"
if [ -z "$nginx_old_id" ]; then
  echo "findb_aws_deploy=failed reason=nginx_container_missing" >&2
  exit 1
fi
nginx_old_project="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$nginx_old_id" </dev/null)"
nginx_old_service="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$nginx_old_id" </dev/null)"
if [ "$nginx_old_project" != "$nginx_expected_project" ] \
  || [ "$nginx_old_service" != "$nginx_expected_service" ]; then
  echo "findb_aws_deploy=failed reason=nginx_container_identity_invalid" >&2
  exit 1
fi
nginx_old_started_at="$(docker inspect --format '{{.State.StartedAt}}' "$nginx_old_id" </dev/null)"
docker stop --time 30 "$nginx_old_id" </dev/null >/dev/null
docker rm "$nginx_old_id" </dev/null >/dev/null
if docker inspect "$nginx_old_id" </dev/null >/dev/null 2>&1; then
  echo "findb_aws_deploy=failed reason=nginx_old_container_still_exists" >&2
  exit 1
fi
docker compose -f "$compose_file" up -d --no-deps nginx </dev/null >/dev/null
nginx_new_id="$(docker compose -f "$compose_file" ps -q nginx </dev/null)"
if [ -z "$nginx_new_id" ] || [ "$nginx_new_id" = "$nginx_old_id" ]; then
  echo "findb_aws_deploy=failed reason=nginx_container_not_replaced" >&2
  exit 1
fi
nginx_new_started_at="$(docker inspect --format '{{.State.StartedAt}}' "$nginx_new_id" </dev/null)"
if [ "$nginx_new_started_at" = "$nginx_old_started_at" ]; then
  echo "findb_aws_deploy=failed reason=nginx_started_at_unchanged" >&2
  exit 1
fi
nginx_new_project="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$nginx_new_id" </dev/null)"
nginx_new_service="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$nginx_new_id" </dev/null)"
nginx_serve_key_mount="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/etc/nginx/serve-key.conf"}}{{.Source}} {{.RW}}{{end}}{{end}}' "$nginx_new_id" </dev/null)"
if [ "$nginx_new_project" != "$nginx_expected_project" ] \
  || [ "$nginx_new_service" != "$nginx_expected_service" ] \
  || [ "$nginx_serve_key_mount" != "$nginx_serve_key_source false" ]; then
  echo "findb_aws_deploy=failed reason=nginx_replacement_contract_invalid" >&2
  exit 1
fi
ready=0
for attempt in 1 2 3 4 5 6; do
  health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' findb-nginx </dev/null 2>/dev/null || true)"
  if [ "$health_status" = "healthy" ]; then
    ready=1
    break
  fi
  sleep 5
done
if [ "$ready" -ne 1 ]; then
  echo "findb_aws_deploy=failed reason=nginx_not_ready" >&2
  exit 1
fi

for dashboard_path in /dashboard/ /dashboard/lookup; do
  docker compose -f "$compose_file" exec -T nginx \
    wget -q --no-check-certificate --spider "https://127.0.0.1$dashboard_path" </dev/null
done

# Prove the recreated nginx container reads the current tmpfs lookup key.
# This is a small, read-only request that only receives the key through the
# exact same-origin Referer map used by Dashboard lookup requests.
lookup_referer="https://$FINDB_PUBLIC_HOST/dashboard/lookup"
docker compose -f "$compose_file" exec -T nginx \
  wget -q --no-check-certificate --spider \
    --header="Referer: $lookup_referer" \
    "https://127.0.0.1/api/v1/serve/instruments?include_count=false&page_size=1" </dev/null

docker compose -f "$compose_file" exec -T serve \
  sh -lc 'python /app/scripts/generate_instrument_cache.py' </dev/null >/dev/null
docker compose -f "$compose_file" exec -T serve \
  sh -lc 'python -c "import os, urllib.request; port=os.getenv(\"PORT\", \"8080\"); urllib.request.urlopen(f\"http://127.0.0.1:{port}/health\", timeout=5)"' \
  </dev/null >/dev/null
if [ -n "${FINDB_RELEASE_ROOT:-}" ]; then
  for dashboard_path in /dashboard/ /dashboard/lookup; do
    curl --fail --silent --show-error --max-time 10 --proto '=https' \
      "https://${FINDB_PUBLIC_HOST}${dashboard_path}" >/dev/null
  done
fi
docker image prune -af --filter "until=168h" </dev/null >/dev/null
echo "findb_aws_deploy=candidate_checks_passed image_refs=exact-digests"
UP_SCRIPT

if [ "$deploy_mode" = activate ]; then
  sudo "$release_root/infra/deploy/runtime-secrets/install_findb_bootstrap.sh" \
    "$AWS_REGION" "$FINDB_PUBLIC_HOST" "$release_root" "$DEPLOYMENT_TARGET" "$AWS_ACCOUNT_ID"
  sudo ln -sfn "$release_root" /opt/findb/current
  transaction_finalized=1
  echo "findb_aws_deploy=activated image_refs=exact-digests"
  exit 0
fi

# Legacy production compatibility path.
transaction_finalized=1
echo "findb_aws_deploy=ready image_refs=exact-digests"
