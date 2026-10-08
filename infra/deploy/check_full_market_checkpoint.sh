#!/usr/bin/env bash
# Read-only installed/recovery checkpoint gate, independent of accepted bundles.
set -euo pipefail
# A failed inspect is not proof of absence: Docker uses rc=1 for daemon and
# permission errors too. Only a successful exact-name inventory can prove absence.
runtime_inventory_recovery=none
runtime_inventory_failure() {
  echo "fetcher_runtime_inventory=failed reason=query_unknown" >&2
  case "$runtime_inventory_recovery" in
    transaction) abort_transaction 1 ;;
    helper) recover 1 ;;
    transaction_recovery) echo "fetcher_aws_deploy=failed reason=rollback_inventory_unverified" >&2; exit 1 ;;
    helper_recovery) echo "release_fetcher_provider=failed reason=recovery_failed" >&2; exit 1 ;;
    *) exit 1 ;;
  esac
}
runtime_exists() {
  local requested="$1" name
  while IFS= read -r name; do
    if [ "$name" = "$requested" ]; then
      docker container inspect "$requested" >/dev/null 2>&1 || runtime_inventory_failure
      return 0
    fi
  done <<< "$runtime_inventory"
  return 1
}

runtime_command_role() {
  local role
  role="$(python3 - "$1" <<'COMMAND'
import json
import sys
try:
    command = json.loads(sys.argv[1])
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
        raise ValueError("invalid command")
    roles = {
        "findb-fetch-full-market": "full_market",
        "findb-fetch-scheduler": "pilot",
        "findb-fetch-finlab-scheduler": "pilot",
        "findb-fetch-shioaji-scheduler": "pilot",
        "findb-fetch-taifex-pilot": "pilot",
        "findb-fetch-historical-backfill": "historical",
    }
    if command == ["python", "-m", "findb_fetcher"]:
        role = "readiness"
    else:
        role = roles[command[0]]
    print(role)
except Exception:
    raise SystemExit(1)
COMMAND
)" || {
    echo "fetcher_runtime_inventory=failed reason=command_unknown" >&2
    return 1
  }
  printf '%s\n' "$role"
}

: "${APP_ENVIRONMENT:?APP_ENVIRONMENT is required}"
if [[ ! "$APP_ENVIRONMENT" =~ ^(staging|production)$ ]] || { [ "${DEPLOYMENT_TARGET+x}" = x ] && [ "$DEPLOYMENT_TARGET" != "$APP_ENVIRONMENT" ]; }; then
  echo "fetcher_checkpoint_gate=failed reason=environment_invalid" >&2
  exit 1
fi
full_market_state=/var/lib/findb-full-market/$APP_ENVIRONMENT
[ "$APP_ENVIRONMENT" != production ] || full_market_state=/var/lib/findb-full-market
require_full_checkpoint() {
  # Installed workers own durable prepared work. Never replace a lost checkpoint,
  # including when an accepted replay invokes an older helper/image.
  if ! sudo test -f "$full_market_state/state.sqlite3" \
    || sudo test -L "$full_market_state/state.sqlite3" \
    || sudo test -L "$full_market_state"; then
    echo "fetcher_aws_deploy=failed reason=full_market_checkpoint_missing" >&2
    return 1
  fi
  if ! sudo python3 - "$full_market_state/state.sqlite3" <<'CHECKPOINT'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
try:
    with closing(sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("checkpoint integrity failed")
        core_columns = {'full_work': 'key,provider,plan_id,member_key,status,attempts,lease_until,body,body_sha256,receipt,reason,next_at', 'full_plan': 'plan_id,provider,dataset,trade_date,body,complete,late', 'full_quota': 'account,window,requests,bytes,blocked_until', 'full_cursor': 'provider,dataset,trade_date'}
        for table, columns in core_columns.items():
            db.execute(f"SELECT {columns} FROM {table} LIMIT 0")
except Exception:
    raise SystemExit(1)
CHECKPOINT
  then
    echo "fetcher_aws_deploy=failed reason=full_market_checkpoint_invalid" >&2
    return 1
  fi
}
# One successful read-only snapshot is the sole absence proof. Every listed
# managed name still requires a successful inspect before command/role checks.
runtime_inventory="$(docker container ls --all --format '{{.Names}}' 2>/dev/null)" || runtime_inventory_failure
retained_full_market=false
for pair in "findb-fetcher-scheduler:findb-full-market-twelve-data" "findb-fetcher-finlab-scheduler:findb-full-market-finlab" "findb-fetcher-shioaji-scheduler:findb-full-market-shioaji" "findb-fetcher-taifex-scheduler:findb-full-market-taifex"; do
  pilot_name="${pair%%:*}"
  full_name="${pair#*:}"
  for base in "$full_name" "$pilot_name" "${full_name}-historical" "${pilot_name}-historical"; do
    for installed in "$base" "${base}-previous" "${base}-candidate" "${base}-transaction-backup" "${base}-legacy-previous" "${base}-preflight" "${base}-preflight-state" "${base}-preflight-control"; do
      if runtime_exists "$installed"; then
        installed_command="$(docker inspect --format '{{json .Config.Cmd}}' "$installed")" || runtime_inventory_failure
        installed_role="$(runtime_command_role "$installed_command")" || runtime_inventory_failure
        if [[ "$installed" == "$full_name"* ]] || [ "$installed_role" = full_market ]; then
          require_full_checkpoint
          retained_full_market=true
        fi
      fi
    done
  done
done
