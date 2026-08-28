#!/usr/bin/env bash
# Load the nginx consumer on the target and render its lookup key into tmpfs.

set -euo pipefail
set +x

catalog="${1:?catalog path required}"
region="${2:?AWS region required}"
public_host="${3:?public host required}"
runtime_dir="${catalog%/*}"
runtime_command="$runtime_dir/runtime_secret_command.sh"
output=/run/findb-runtime-secrets/nginx/serve-key.conf

case "$catalog" in
  /opt/*/runtime-secrets/*.json) ;;
  *)
    echo "render_nginx_runtime=failed reason=catalog_path_invalid" >&2
    exit 1
    ;;
esac

"$runtime_command" \
  --catalog "$catalog" \
  --region "$region" \
  --consumer nginx \
  -- bash -s -- "$public_host" "$output" <<'RENDER_SCRIPT'
set -euo pipefail
public_host="$1"
output="$2"
rm -f -- "$output"
printf '%s\n' "$FINDB_LOOKUP_SERVE_API_KEY" | \
  python3 /opt/findb/runtime-secrets/render_serve_key.py \
    --public-host "$public_host" \
    --output "$output"
chown 0:0 "$output"
chmod 0600 "$output"
if [ "$(stat -c '%u:%g:%a' "$output")" != "0:0:600" ]; then
  echo "render_nginx_runtime=failed reason=output_metadata_invalid" >&2
  exit 1
fi
if [ "$(findmnt -n -o FSTYPE -T "$output")" != "tmpfs" ]; then
  echo "render_nginx_runtime=failed reason=output_not_tmpfs" >&2
  exit 1
fi
echo "render_nginx_runtime=ready output=tmpfs"
RENDER_SCRIPT
