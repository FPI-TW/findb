#!/usr/bin/env bash
# Load the nginx consumer on the target and render its lookup key into tmpfs.

set -euo pipefail
set +x

catalog="${1:?catalog path required}"
region="${2:?AWS region required}"
public_host="${3:?public host required}"
deployment_target="${4:?deployment target required}"
aws_account_id="${5:?AWS account id required}"
render_mode="${6:-materialize}"
runtime_output_dir=/run/findb-runtime-secrets/nginx
serve_key_output="$runtime_output_dir/serve-key.conf"
certificate_output="$runtime_output_dir/server.crt"
private_key_output="$runtime_output_dir/server.key"

case "$render_mode" in
  validate|materialize) ;;
  *) echo "render_nginx_runtime=failed reason=render_mode_invalid" >&2; exit 1 ;;
esac

if [[ "$catalog" =~ ^/opt/findb/releases/[0-9a-f]{64}-[0-9]+-[0-9]+/infra/deploy/runtime-secrets/findb\.json$ ]] \
  || [ "$catalog" = /opt/findb/runtime-secrets/findb.json ]; then
  runtime_dir="${catalog%/findb.json}"
elif [ "$deployment_target" = staging ] \
  && [[ "$catalog" =~ ^/opt/findb/releases/[0-9a-f]{64}-[0-9]+-[0-9]+-findb/infra/deploy/runtime-secrets/findb\.json$ ]]; then
  # Keep accepted staging v1 bundles replayable through their original release identity.
  runtime_dir="${catalog%/findb.json}"
else
  echo "render_nginx_runtime=failed reason=catalog_path_invalid" >&2
  exit 1
fi
runtime_command="$runtime_dir/runtime_secret_command.sh"
renderer="$runtime_dir/render_serve_key.py"

if [ ! -f "$renderer" ] || [ -L "$renderer" ] \
  || [ "$(stat -c '%u:%g:%a' "$renderer")" != "0:0:755" ]; then
  echo "render_nginx_runtime=failed reason=renderer_metadata_invalid" >&2
  exit 1
fi

"$runtime_command" \
  --catalog "$catalog" \
  --region "$region" \
  --deployment-target "$deployment_target" \
  --aws-account-id "$aws_account_id" \
  --consumer nginx \
  -- bash -s -- \
    "$public_host" "$serve_key_output" "$certificate_output" "$private_key_output" \
    "$renderer" "$deployment_target" "$render_mode" <<'RENDER_SCRIPT'
set -euo pipefail
public_host="$1"
serve_key_output="$2"
certificate_output="$3"
private_key_output="$4"
renderer="$5"
deployment_target="$6"
render_mode="$7"
runtime_output_dir="${serve_key_output%/*}"
umask 077
certificate_tmp=""
private_key_tmp=""
cleanup() {
  status=$?
  set +e
  rm -f -- "$certificate_tmp" "$private_key_tmp"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
if [ "$render_mode" = materialize ]; then
  rm -f -- "$serve_key_output"
  printf '%s\n' "$FINDB_LOOKUP_SERVE_API_KEY" | \
    python3 "$renderer" \
      --public-host "$public_host" \
      --output "$serve_key_output"
  chown 0:0 "$serve_key_output"
  chmod 0600 "$serve_key_output"
  if [ "$(stat -c '%u:%g:%a' "$serve_key_output")" != "0:0:600" ]; then
    echo "render_nginx_runtime=failed reason=output_metadata_invalid" >&2
    exit 1
  fi
  if [ "$(findmnt -n -o FSTYPE -T "$serve_key_output")" != "tmpfs" ]; then
    echo "render_nginx_runtime=failed reason=output_not_tmpfs" >&2
    exit 1
  fi
fi

if [ "$deployment_target" = production ]; then
  certificate_tmp="$(mktemp "$runtime_output_dir/server.crt.XXXXXX")"
  private_key_tmp="$(mktemp "$runtime_output_dir/server.key.XXXXXX")"
  if ! printf '%s' "$FINDB_ORIGIN_CERTIFICATE_PEM_B64" | base64 --decode >"$certificate_tmp" \
    || ! printf '%s' "$FINDB_ORIGIN_PRIVATE_KEY_PEM_B64" | base64 --decode >"$private_key_tmp"; then
    echo "render_nginx_runtime=failed reason=tls_base64_invalid" >&2
    exit 1
  fi
  if ! openssl x509 -in "$certificate_tmp" -noout -checkhost "$public_host" >/dev/null 2>&1 \
    || ! openssl x509 -in "$certificate_tmp" -noout -checkend 2592000 >/dev/null 2>&1 \
    || ! openssl pkey -in "$private_key_tmp" -noout >/dev/null 2>&1; then
    echo "render_nginx_runtime=failed reason=tls_material_invalid" >&2
    exit 1
  fi
  certificate_public_key="$(openssl x509 -in "$certificate_tmp" -pubkey -noout | openssl sha256)"
  private_public_key="$(openssl pkey -in "$private_key_tmp" -pubout 2>/dev/null | openssl sha256)"
  if [ "$certificate_public_key" != "$private_public_key" ]; then
    echo "render_nginx_runtime=failed reason=tls_keypair_mismatch" >&2
    exit 1
  fi
  chown 0:0 "$certificate_tmp" "$private_key_tmp"
  chmod 0600 "$certificate_tmp" "$private_key_tmp"
  for tls_tmp in "$certificate_tmp" "$private_key_tmp"; do
    if [ "$(stat -c '%u:%g:%a' "$tls_tmp")" != "0:0:600" ] \
      || [ "$(findmnt -n -o FSTYPE -T "$tls_tmp")" != "tmpfs" ]; then
      echo "render_nginx_runtime=failed reason=tls_output_unsafe" >&2
      exit 1
    fi
  done
  if [ "$render_mode" = materialize ]; then
    mv -f -- "$certificate_tmp" "$certificate_output"
    certificate_tmp=""
    mv -f -- "$private_key_tmp" "$private_key_output"
    private_key_tmp=""
  fi
fi

echo "render_nginx_runtime=ready mode=$render_mode outputs=tmpfs"
RENDER_SCRIPT
