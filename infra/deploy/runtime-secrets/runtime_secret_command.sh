#!/usr/bin/env bash
# Run one bounded host command with an allowlisted Secrets Manager consumer set.
# This file intentionally contains no secret values.

set -euo pipefail
set +x
umask 077

usage() {
  echo "usage: runtime_secret_command.sh --catalog PATH --region REGION --consumer NAME [--consumer NAME ...] [--map SOURCE=DEST] [--ecr-registry REGISTRY] --docker-login -- COMMAND [ARG ...]" >&2
  exit 2
}

catalog=""
region=""
declare -a consumers=()
declare -a mappings=()
docker_login_requested=0
ecr_registry=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --catalog)
      [ "$#" -ge 2 ] || usage
      catalog="$2"
      shift 2
      ;;
    --region)
      [ "$#" -ge 2 ] || usage
      region="$2"
      shift 2
      ;;
    --consumer)
      [ "$#" -ge 2 ] || usage
      consumers+=("$2")
      shift 2
      ;;
    --map)
      [ "$#" -ge 2 ] || usage
      mappings+=("$2")
      shift 2
      ;;
    --docker-login)
      docker_login_requested=1
      shift
      ;;
    --ecr-registry)
      [ "$#" -ge 2 ] || usage
      ecr_registry="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      usage
      ;;
  esac
done

[ -n "$catalog" ] || usage
[ -n "$region" ] || usage
[ "${#consumers[@]}" -gt 0 ] || usage
[ "$#" -gt 0 ] || usage

if [ "$region" != "ap-southeast-1" ]; then
  echo "runtime_secret_command=failed reason=region_invalid" >&2
  exit 1
fi

case "$catalog" in
  /opt/*/runtime-secrets/*.json) ;;
  *)
    echo "runtime_secret_command=failed reason=catalog_path_invalid" >&2
    exit 1
    ;;
esac

runtime_root=/run/findb-runtime-secrets
loader="${catalog%/*}/load_runtime_secrets.py"
case "$loader" in
  /opt/*/runtime-secrets/load_runtime_secrets.py) ;;
  *)
    echo "runtime_secret_command=failed reason=loader_path_invalid" >&2
    exit 1
    ;;
  esac

if [ "$(id -u)" -ne 0 ] \
  || [ ! -f "$loader" ] \
  || [ -L "$loader" ] \
  || [ "$(stat -c '%u:%g:%a' "$loader" 2>/dev/null || true)" != "0:0:755" ] \
  || [ ! -f "$catalog" ] \
  || [ -L "$catalog" ] \
  || [ "$(stat -c '%u:%g:%a' "$catalog" 2>/dev/null || true)" != "0:0:644" ]; then
  echo "runtime_secret_command=failed reason=bundle_metadata_invalid" >&2
  exit 1
fi

if ! command -v findmnt >/dev/null 2>&1 || [ "$(findmnt -n -o FSTYPE -T "$runtime_root" 2>/dev/null || true)" != "tmpfs" ]; then
  echo "runtime_secret_command=failed reason=runtime_root_not_tmpfs" >&2
  exit 1
fi

declare -a loaded_files=()
declare -a loaded_keys=()
docker_config=""
docker_login_done=0

cleanup() {
  status=$?
  set +e
  trap - EXIT INT TERM HUP
  for file in "${loaded_files[@]}"; do
    [ -e "$file" ] && rm -f -- "$file"
  done
  if [ "$docker_login_done" -eq 1 ]; then
    docker logout "$ecr_registry" >/dev/null 2>&1 || true
  fi
  if [ -n "$docker_config" ]; then
    rm -rf -- "$docker_config"
  fi
  for key in "${loaded_keys[@]}"; do
    unset "$key"
  done
  unset DOCKER_CONFIG
  exit "$status"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

load_consumer() {
  local consumer="$1"
  local output="$runtime_root/$consumer/runtime.env"
  local owner
  local key
  local value

  if ! [[ "$consumer" =~ ^[a-z0-9-]+$ ]]; then
    echo "runtime_secret_command=failed reason=consumer_invalid" >&2
    exit 1
  fi
  owner="$(id -u):$(id -g)"
  python3 "$loader" \
    --catalog "$catalog" \
    --consumer "$consumer" \
    --region "$region" \
    --output "$output" \
    --owner "$owner"
  if [ ! -f "$output" ] || [ -L "$output" ] || [ "$(stat -c '%u:%g:%a' "$output" 2>/dev/null || true)" != "0:0:600" ]; then
    echo "runtime_secret_command=failed reason=loader_output_unsafe" >&2
    exit 1
  fi
  if [ "$(findmnt -n -o FSTYPE -T "$output" 2>/dev/null || true)" != "tmpfs" ]; then
    echo "runtime_secret_command=failed reason=loader_output_not_tmpfs" >&2
    exit 1
  fi

  # Register the path before parsing/sourcing so every failure path unlinks it.
  loaded_files+=("$output")

  while IFS='=' read -r key value || [ -n "$key" ]; do
    if ! [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
      echo "runtime_secret_command=failed reason=loader_output_invalid" >&2
      exit 1
    fi
    loaded_keys+=("$key")
  done < "$output"

  # Source only this checked, loader-created file, then unlink it immediately.
  set -a
  . "$output"
  set +a
  rm -f -- "$output"
}

for consumer in "${consumers[@]}"; do
  load_consumer "$consumer"
done

for mapping in "${mappings[@]}"; do
  source_name="${mapping%%=*}"
  destination_name="${mapping#*=}"
  if [ "$source_name" = "$mapping" ] \
    || ! [[ "$source_name" =~ ^[A-Z_][A-Z0-9_]*$ ]] \
    || ! [[ "$destination_name" =~ ^[A-Z_][A-Z0-9_]*$ ]] \
    || [ -z "${!source_name+x}" ]; then
    echo "runtime_secret_command=failed reason=environment_mapping_invalid" >&2
    exit 1
  fi
  printf -v "$destination_name" '%s' "${!source_name}"
  export "$destination_name"
  loaded_keys+=("$destination_name")
  unset "$source_name"
done

if [ "$docker_login_requested" -eq 1 ]; then
  if ! [[ "$ecr_registry" =~ ^439622209937\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com$ ]]; then
    echo "runtime_secret_command=failed reason=ecr_registry_invalid" >&2
    exit 1
  fi
  docker_config="$(mktemp -d "$runtime_root/docker-config.XXXXXX")"
  chmod 700 "$docker_config"
  export DOCKER_CONFIG="$docker_config"
  if ! aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$ecr_registry" >/dev/null; then
    echo "runtime_secret_command=failed reason=ecr_login_failed" >&2
    exit 1
  fi
  docker_login_done=1
fi

"$@"
