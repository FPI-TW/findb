#!/usr/bin/env bash
# FinDB staging AWS-mode deployment. Runtime secrets are loaded on the host
# from the instance role and are never supplied by the runner.

set -euo pipefail
set +x

runtime_dir=/opt/findb/runtime-secrets
catalog="$runtime_dir/findb.json"
runtime_command="$runtime_dir/runtime_secret_command.sh"
compose_file=/opt/findb/docker-compose.prod.yml
nginx_runtime="$runtime_dir/render_nginx_runtime.sh"

: "${AWS_REGION:?AWS_REGION is required}"
: "${IMAGE_TAG:?IMAGE_TAG is required}"
: "${DASHBOARD_IMAGE:?DASHBOARD_IMAGE is required}"
: "${FINDB_PUBLIC_HOST:?FINDB_PUBLIC_HOST is required}"
: "${ECR_REGISTRY:?ECR_REGISTRY is required}"
: "${FINDB_IMAGE:?FINDB_IMAGE is required}"

if [ "$AWS_REGION" != "ap-southeast-1" ]; then
  echo "findb_aws_deploy=failed reason=region_invalid" >&2
  exit 1
fi

expected_ecr_registry="439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
expected_findb_image="$expected_ecr_registry/findb/staging/backend"
expected_dashboard_image="$expected_ecr_registry/findb/staging/dashboard"

if [ "$ECR_REGISTRY" != "$expected_ecr_registry" ] \
  || [ "$FINDB_IMAGE" != "$expected_findb_image" ] \
  || [ "$DASHBOARD_IMAGE" != "$expected_dashboard_image" ]; then
  echo "findb_aws_deploy=failed reason=ecr_image_contract" >&2
  exit 1
fi
if ! printf '%s' "$IMAGE_TAG" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "findb_aws_deploy=failed reason=image_tag_not_lowercase_sha" >&2
  exit 1
fi

# Only nonsecret deployment settings are preserved across sudo. The runtime
# command itself creates and loads the secret environment after this boundary.
preserve_env=AWS_REGION,IMAGE_TAG,ECR_REGISTRY,FINDB_IMAGE,DASHBOARD_IMAGE,FINDB_PUBLIC_HOST,COMPOSE_FILE,APP_NAME,APP_VERSION,DEBUG,PORT,DATABASE_POOL_SIZE,DATABASE_MAX_OVERFLOW,API_V1_PREFIX,API_KEY_HEADER,SOURCE_ALLOWLIST_CIDRS,SOURCE_TRUST_PROXY_HEADERS,SERVE_REQUIRE_AUTH,RATE_LIMIT_REQUESTS,RATE_LIMIT_WINDOW,RAW_RETENTION_ENABLED,RAW_RETENTION_DAYS,FINDB_STATIC_CACHE_BASE_URL,FINDB_LATEST_PRICE_WORKERS,CLOUDFLARE_R2_ACCOUNT_ID,CLOUDFLARE_R2_CANONICAL_BUCKET

run_runtime() {
  sudo --preserve-env="$preserve_env" "$runtime_command" \
    --catalog "$catalog" \
    --region "$AWS_REGION" \
    "$@"
}

if [ ! -f "$compose_file" ]; then
  echo "findb_aws_deploy=failed reason=compose_missing" >&2
  exit 1
fi
for conf in nginx.conf source-allowlist.conf cloudflare-real-ip.conf; do
  if [ ! -f "/home/ubuntu/etc/nginx/$conf" ]; then
    echo "findb_aws_deploy=failed reason=nginx_config_missing" >&2
    exit 1
  fi
done
for tls_file in server.crt server.key; do
  if [ ! -f "/home/ubuntu/etc/nginx/ssl/$tls_file" ]; then
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

# The lookup key is rendered on the host into /run (tmpfs), never in the
# runner checkout or a persistent host env/config directory.
sudo --preserve-env="$preserve_env" "$nginx_runtime" "$catalog" "$AWS_REGION" "$FINDB_PUBLIC_HOST"

run_runtime --consumer migration --consumer compose --map MIGRATION_DATABASE_URL=DATABASE_URL -- bash -s -- "$compose_file" <<'MIGRATION_CHECK_SCRIPT'
set -euo pipefail
compose_file="$1"
docker compose -f "$compose_file" run --rm --no-deps ingest \
  python /app/scripts/predeploy_db_check.py
MIGRATION_CHECK_SCRIPT

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
run_runtime --consumer migration --consumer compose --map MIGRATION_DATABASE_URL=DATABASE_URL -- bash -s -- "$compose_file" <<'MIGRATION_SCRIPT'
set -euo pipefail
compose_file="$1"
docker compose -f "$compose_file" run --rm --no-deps ingest sh -euc '
  uv run alembic upgrade head
  uv run alembic current
'
docker compose -f "$compose_file" run --rm --no-deps ingest \
  python /app/scripts/provision_registry.py \
  --deployment-target staging
MIGRATION_SCRIPT

run_runtime --consumer compose -- bash -s -- "$compose_file" <<'UP_SCRIPT'
set -euo pipefail
compose_file="$1"
docker compose -f "$compose_file" up -d --remove-orphans </dev/null

if ! docker compose -f "$compose_file" exec -T rabbitmq rabbitmq-diagnostics -q ping </dev/null >/dev/null; then
  echo "findb_aws_deploy=failed reason=rabbitmq_unhealthy" >&2
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
docker image prune -af --filter "until=168h" </dev/null >/dev/null
echo "findb_aws_deploy=ready image_tag=$IMAGE_TAG"
UP_SCRIPT
