#!/usr/bin/env bash
# Install a nonsecret boot-time dependency that recreates the nginx lookup key
# in /run before Docker restores the FinDB containers.

set -euo pipefail
set +x

region="${1:?AWS region required}"
public_host="${2:?FinDB public host required}"
release_root="${3:-/opt/findb}"
deployment_target="${4:?deployment target required}"
aws_account_id="${5:?AWS account id required}"

if [ "$(id -u)" -ne 0 ]; then
  echo "install_findb_bootstrap=failed reason=root_required" >&2
  exit 1
fi
if [ "$region" != "ap-southeast-1" ]; then
  echo "install_findb_bootstrap=failed reason=region_invalid" >&2
  exit 1
fi
case "$deployment_target" in staging|production) ;; *) echo "install_findb_bootstrap=failed reason=deployment_target_invalid" >&2; exit 1 ;; esac
if ! [[ "$aws_account_id" =~ ^[0-9]{12}$ ]]; then
  echo "install_findb_bootstrap=failed reason=aws_account_invalid" >&2
  exit 1
fi
if ! [[ "$public_host" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
  echo "install_findb_bootstrap=failed reason=public_host_invalid" >&2
  exit 1
fi

unit_path=/etc/systemd/system/findb-runtime-nginx.service
dropin_dir=/etc/systemd/system/docker.service.d
dropin_path="$dropin_dir/findb-runtime-secrets.conf"
unit_tmp="$(mktemp /run/findb-runtime-nginx.service.XXXXXX)"
dropin_tmp="$(mktemp /run/findb-runtime-docker-dropin.XXXXXX)"

cleanup() {
  status=$?
  set +e
  rm -f -- "$unit_tmp" "$dropin_tmp"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

cat >"$unit_tmp" <<UNIT
[Unit]
Description=Materialize FinDB nginx runtime secret into tmpfs
Wants=network-online.target
After=network-online.target
Before=docker.service

[Service]
Type=oneshot
ExecStartPre=/usr/bin/install -d -o root -g root -m 0700 /run/findb-runtime-secrets
ExecStartPre=/usr/bin/install -d -o root -g root -m 0700 /run/findb-runtime-secrets/nginx
ExecStart=$release_root/infra/deploy/runtime-secrets/render_nginx_runtime.sh $release_root/infra/deploy/runtime-secrets/findb.json $region $public_host $deployment_target $aws_account_id

[Install]
WantedBy=multi-user.target
UNIT

cat >"$dropin_tmp" <<'DROPIN'
[Unit]
Requires=findb-runtime-nginx.service
After=findb-runtime-nginx.service
DROPIN

install -d -o root -g root -m 0755 "$dropin_dir"
install -o root -g root -m 0644 "$unit_tmp" "$unit_path"
install -o root -g root -m 0644 "$dropin_tmp" "$dropin_path"
if [ "$(stat -c '%u:%g:%a' "$unit_path")" != "0:0:644" ] \
  || [ "$(stat -c '%u:%g:%a' "$dropin_path")" != "0:0:644" ]; then
  echo "install_findb_bootstrap=failed reason=unit_metadata_invalid" >&2
  exit 1
fi

systemd-analyze verify "$unit_path" docker.service >/dev/null
systemctl daemon-reload
systemctl enable findb-runtime-nginx.service >/dev/null
if [ "$(systemctl is-enabled findb-runtime-nginx.service)" != "enabled" ]; then
  echo "install_findb_bootstrap=failed reason=unit_not_enabled" >&2
  exit 1
fi
echo "install_findb_bootstrap=ready unit=findb-runtime-nginx.service"
