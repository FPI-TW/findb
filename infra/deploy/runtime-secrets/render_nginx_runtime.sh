#!/usr/bin/env bash
# Load the nginx consumer on the target and render its lookup key into tmpfs.

set -euo pipefail
set +x

catalog="${1:?catalog path required}"
region="${2:?AWS region required}"
public_host="${3:?public host required}"
output=/run/findb-runtime-secrets/nginx/serve-key.conf

if [[ "$catalog" =~ ^/opt/findb/releases/[0-9a-f]{64}-[0-9]+-[0-9]+-findb/infra/deploy/runtime-secrets/findb\.json$ ]] \
  || [ "$catalog" = /opt/findb/runtime-secrets/findb.json ]; then
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
  --consumer nginx \
  -- bash -s -- "$public_host" "$output" "$renderer" <<'RENDER_SCRIPT'
set -euo pipefail
public_host="$1"
output="$2"
renderer="$3"
rm -f -- "$output"
printf '%s\n' "$FINDB_LOOKUP_SERVE_API_KEY" | \
  python3 "$renderer" \
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
