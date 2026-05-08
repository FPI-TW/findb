#!/usr/bin/env bash
# Cron-friendly wrapper for daily TWStock ingest.
#
# Schedule (weekday 15:00 Asia/Taipei) via crontab:
#   0 15 * * 1-5 /mnt/c/Users/User/Desktop/Project/findb/scripts/run_daily_ingest.sh
#
# Env you can override:
#   FINDB_BASE_URL        default: https://findb.tingfong.com
#   FINDB_DAILY_ROOT      default: /mnt/c/Users/User/Downloads/TWStock/daily
#   FINDB_REPO_DIR        default: /mnt/c/Users/User/Desktop/Project/findb
#   FINDB_LOG_DIR         default: <repo>/.ingest_log_daily
#
# The wrapper:
#   * cd's into the repo so uv finds .env / pyproject
#   * adds ~/.local/bin to PATH (cron has minimal PATH)
#   * writes a timestamped per-run log under <log_dir>/runs/
#   * forwards uv run output, exit code preserved
set -euo pipefail

export PATH="${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"

REPO_DIR="${FINDB_REPO_DIR:-/mnt/c/Users/User/Desktop/Project/findb}"
DAILY_ROOT="${FINDB_DAILY_ROOT:-/mnt/c/Users/User/Downloads/TWStock/daily}"
BASE_URL="${FINDB_BASE_URL:-https://findb.tingfong.com}"
LOG_DIR="${FINDB_LOG_DIR:-${REPO_DIR}/.ingest_log_daily}"

cd "${REPO_DIR}"

# Load cron-only secrets (e.g. prod SOURCE_API_KEY) if present.
# .env.cron is gitignored and overrides .env via export.
if [ -f "${REPO_DIR}/.env.cron" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${REPO_DIR}/.env.cron"
  set +a
fi

mkdir -p "${LOG_DIR}/runs"
RUN_LOG="${LOG_DIR}/runs/daily_ingest_$(date +%Y%m%d_%H%M%S).log"

{
  echo "==== run started at $(date -Iseconds) ===="
  echo "REPO_DIR=${REPO_DIR}"
  echo "DAILY_ROOT=${DAILY_ROOT}"
  echo "BASE_URL=${BASE_URL}"
  echo "LOG_DIR=${LOG_DIR}"

  uv run python scripts/bulk_ingest_twstock.py \
    --root "${DAILY_ROOT}" \
    --base-url "${BASE_URL}" \
    --log-dir "${LOG_DIR}" \
    --concurrency 1 \
    --retry 5

  RC=$?
  echo "==== run finished at $(date -Iseconds) rc=${RC} ===="
  exit "${RC}"
} >>"${RUN_LOG}" 2>&1
