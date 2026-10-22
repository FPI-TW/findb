#!/usr/bin/env bash
# Keep one 4 GiB swap file on a Fetcher host. Idempotent for SSM State Manager:
# swap only absorbs short memory peaks, so it is used under pressure, not as RAM.

set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

swap=/swapfile
size_bytes=4294967296

active() {
  swapon --show=NAME --noheadings | grep -qx "$swap"
}

if ! active; then
  if [ ! -f "$swap" ] || [ "$(stat -c %s "$swap")" -ne "$size_bytes" ]; then
    rm -f "$swap"
    fallocate -l "$size_bytes" "$swap"
  fi
  chown root:root "$swap"
  chmod 0600 "$swap"
  mkswap "$swap" >/dev/null
  swapon "$swap"
fi
grep -qE "^$swap[[:space:]]" /etc/fstab || printf '%s none swap sw 0 0\n' "$swap" >> /etc/fstab
printf 'vm.swappiness = 10\n' > /etc/sysctl.d/90-findb-swap.conf
sysctl -q -p /etc/sysctl.d/90-findb-swap.conf
active
echo "fetcher_swap=ready bytes=$size_bytes"
