#!/bin/sh
set -eu

port="${PORT:-8080}"
host="${HOST:-0.0.0.0}"
generate_cache="${FINDB_GENERATE_STATIC_CACHE_ON_STARTUP:-true}"
cache_attempts="${FINDB_STATIC_CACHE_STARTUP_ATTEMPTS:-24}"
cache_base_url="${FINDB_STATIC_CACHE_BASE_URL:-http://127.0.0.1:${port}}"

uvicorn app.main:app --host "$host" --port "$port" --reload &
server_pid="$!"

cleanup() {
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
}

trap cleanup INT TERM

if [ "$generate_cache" = "true" ]; then
  (
    echo "Waiting for app readiness before generating static cache"
    attempt=1
    while [ "$attempt" -le "$cache_attempts" ]; do
      if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${port}/health', timeout=5)" >/dev/null 2>&1; then
        echo "Generating static cache from ${cache_base_url}"
        if FINDB_STATIC_CACHE_BASE_URL="$cache_base_url" python /app/scripts/generate_instrument_cache.py; then
          echo "Static cache generated"
        else
          echo "Static cache generation failed; app will keep running" >&2
        fi
        exit 0
      fi

      echo "App not ready for static cache generation (attempt ${attempt}/${cache_attempts}); waiting 5s..."
      attempt=$((attempt + 1))
      sleep 5
    done

    echo "Static cache generation skipped; app did not become ready in time" >&2
  ) &
fi

wait "$server_pid"
