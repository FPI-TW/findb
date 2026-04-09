"""
Generate a static instrument cache for the FinDB instrument lookup page.

This script fetches every page from ``GET /api/v1/serve/instruments`` and writes
the reduced payload to ``app/static/data/instruments.json`` using an atomic
rename so a failed refresh never overwrites the last good cache.

Usage:
    python scripts/generate_instrument_cache.py

Environment variables:
    FINDB_BASE_URL         Base URL for the FinDB app. Default: http://localhost:8080
    FINDB_SERVE_API_KEY    Optional Serve API key when authentication is enabled.

Crontab example:
    0 6 * * * cd /app && python scripts/generate_instrument_cache.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse
from urllib.request import Request, urlopen

BASE_URL = os.getenv("FINDB_BASE_URL", "http://localhost:8080").rstrip("/")
SERVE_API_KEY = os.getenv("FINDB_SERVE_API_KEY", "").strip()
PAGE_SIZE = 1000
TIMEOUT_SECONDS = 30
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "app" / "static" / "data" / "instruments.json"
API_KEY_HEADER = "X-API-Key"
INSTRUMENT_FIELDS = (
    "instrument_id",
    "market",
    "asset_class",
    "symbol",
    "name",
    "currency",
    "status",
)


def normalize_instrument(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields needed by the static lookup page."""
    return {field: payload.get(field) for field in INSTRUMENT_FIELDS}


def sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    """Stable default ordering for the static dataset."""
    market = str(item.get("market") or "")
    symbol = str(item.get("symbol") or "")
    name = str(item.get("name") or "")
    return (market.upper(), symbol.upper(), name.upper())


def build_cache_payload(
    instruments: list[dict[str, Any]],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the final JSON document consumed by the lookup page."""
    timestamp = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ordered = sorted((normalize_instrument(item) for item in instruments), key=sort_key)
    markets = sorted({item["market"] for item in ordered if item.get("market")})
    asset_classes = sorted({item["asset_class"] for item in ordered if item.get("asset_class")})

    return {
        "generated_at": timestamp.isoformat().replace("+00:00", "Z"),
        "total": len(ordered),
        "markets": markets,
        "asset_classes": asset_classes,
        "data": ordered,
    }


def fetch_instruments_page(
    base_url: str,
    page: int,
    page_size: int,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch a single paginated response from the Serve API."""
    query = parse.urlencode({"page": page, "page_size": page_size})
    url = f"{base_url}/api/v1/serve/instruments?{query}"
    headers = {"Accept": "application/json"}

    if api_key:
        headers[API_KEY_HEADER] = api_key

    req = Request(url, headers=headers, method="GET")

    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"API request failed with HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Unable to reach FinDB API at {url}: {exc.reason}") from exc


def collect_instruments(
    base_url: str,
    api_key: str | None = None,
    page_size: int = PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Fetch every page from the Serve API."""
    collected: list[dict[str, Any]] = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        payload = fetch_instruments_page(base_url, page=page, page_size=page_size, api_key=api_key)
        if payload.get("success") is not True:
            raise RuntimeError(f"Serve API returned unsuccessful payload on page {page}")

        page_items = payload.get("data")
        pagination = payload.get("pagination") or {}
        if not isinstance(page_items, list):
            raise RuntimeError(f"Unexpected response format on page {page}: missing data array")

        total_pages = int(pagination.get("total_pages") or 0)
        if total_pages <= 0:
            total_pages = 1 if page_items else 0

        collected.extend(page_items)
        page += 1

    return collected


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON to a temp file and atomically replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix="instruments.",
        suffix=".tmp",
        dir=path.parent,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def print_summary(payload: dict[str, Any]) -> None:
    """Print a concise cache summary to stdout."""
    counts = Counter(item.get("market") or "UNKNOWN" for item in payload["data"])
    print(
        "Instrument cache generated:",
        f"total={payload['total']}",
        f"output={OUTPUT_PATH}",
    )
    for market, count in sorted(counts.items()):
        print(f"  {market}: {count}")


def main() -> int:
    """CLI entry point."""
    try:
        instruments = collect_instruments(BASE_URL, api_key=SERVE_API_KEY or None)
        payload = build_cache_payload(instruments)
        write_json_atomic(OUTPUT_PATH, payload)
        print_summary(payload)
        return 0
    except Exception as exc:
        print(f"Failed to generate instrument cache: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
