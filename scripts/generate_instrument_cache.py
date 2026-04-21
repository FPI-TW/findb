"""
Generate a static instrument cache for the FinDB instrument lookup page.

This script fetches every page from ``GET /api/v1/serve/instruments`` and
``GET /api/v1/serve/macro/series``. It writes reduced static payloads to
``app/static/data`` using atomic renames so a failed refresh never overwrites
the last good cache.

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse
from urllib.request import Request, urlopen

BASE_URL = os.getenv("FINDB_BASE_URL", "http://localhost:8080").rstrip("/")
SERVE_API_KEY = os.getenv("FINDB_SERVE_API_KEY", "").strip()
PAGE_SIZE = 1000
LATEST_PRICE_WORKERS = int(os.getenv("FINDB_LATEST_PRICE_WORKERS", "12"))
TIMEOUT_SECONDS = 30
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "app" / "static" / "data" / "instruments.json"
MACRO_OUTPUT_PATH = PROJECT_ROOT / "app" / "static" / "data" / "macro-series.json"
API_KEY_HEADER = "X-API-Key"
INSTRUMENT_FIELDS = (
    "instrument_id",
    "market",
    "asset_class",
    "symbol",
    "name",
    "short_name",
    "currency",
    "status",
    "latest_trade_date",
    "latest_price",
)
MACRO_SERIES_FIELDS = (
    "series_id",
    "name",
    "unit",
    "frequency",
    "market",
    "source_code",
    "source",
)


def normalize_instrument(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields needed by the static lookup page."""
    return {field: payload.get(field) for field in INSTRUMENT_FIELDS}


def sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    """Stable default ordering for the static dataset."""
    market = str(item.get("market") or "")
    symbol = str(item.get("symbol") or "")
    name = str(item.get("short_name") or item.get("name") or "")
    return (market.upper(), symbol.upper(), name.upper())


def normalize_macro_series(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields needed by the static macro lookup page."""
    return {field: payload.get(field) for field in MACRO_SERIES_FIELDS}


def macro_sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    """Stable default ordering for the static macro dataset."""
    market = str(item.get("market") or "")
    name = str(item.get("name") or "")
    source_code = str(item.get("source_code") or "")
    return (market.upper(), name.upper(), source_code.upper())


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


def build_macro_series_payload(
    series_items: list[dict[str, Any]],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the final JSON document consumed by the macro lookup tab."""
    timestamp = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ordered = sorted(
        (normalize_macro_series(item) for item in series_items),
        key=macro_sort_key,
    )
    markets = sorted({item["market"] for item in ordered if item.get("market")})
    frequencies = sorted({item["frequency"] for item in ordered if item.get("frequency")})
    sources = sorted({item["source"] for item in ordered if item.get("source")})

    return {
        "generated_at": timestamp.isoformat().replace("+00:00", "Z"),
        "total": len(ordered),
        "markets": markets,
        "frequencies": frequencies,
        "sources": sources,
        "data": ordered,
    }


def fetch_page(
    base_url: str,
    endpoint: str,
    page: int,
    page_size: int,
    api_key: str | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch a single paginated response from a Serve API endpoint."""
    query_params: dict[str, Any] = {"page": page, "page_size": page_size}
    if params:
        query_params.update(params)
    query = parse.urlencode(query_params)
    url = f"{base_url}/api/v1/serve/{endpoint}?{query}"
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


def fetch_instruments_page(
    base_url: str,
    page: int,
    page_size: int,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch a single paginated instruments response from the Serve API."""
    return fetch_page(base_url, "instruments", page, page_size, api_key)


def fetch_latest_eod(
    base_url: str,
    instrument_id: str,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    """Fetch the newest EOD record for one instrument."""
    payload = fetch_page(base_url, f"eod/{instrument_id}", page=1, page_size=1, api_key=api_key)
    if payload.get("success") is not True:
        raise RuntimeError(f"EOD API returned unsuccessful payload for {instrument_id}")

    page_items = payload.get("data")
    if not isinstance(page_items, list):
        raise RuntimeError(f"Unexpected EOD response format for {instrument_id}: missing data array")

    return page_items[0] if page_items else None


def fetch_latest_futures_continuous(
    base_url: str,
    market: str,
    symbol: str,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    """Fetch the newest continuous futures record for one symbol."""
    payload = fetch_page(
        base_url,
        "futures/continuous",
        page=1,
        page_size=1,
        api_key=api_key,
        params={"market": market, "symbols": symbol},
    )
    if payload.get("success") is not True:
        raise RuntimeError(f"Futures continuous API returned unsuccessful payload for {market} {symbol}")

    page_items = payload.get("data")
    if not isinstance(page_items, list):
        raise RuntimeError(f"Unexpected futures response format for {market} {symbol}: missing data array")

    return page_items[0] if page_items else None


def fetch_latest_price_snapshot(
    base_url: str,
    instrument: dict[str, Any],
    api_key: str | None = None,
) -> dict[str, Any] | None:
    """Fetch the latest date/price snapshot from the matching read-only Serve endpoint."""
    instrument_id = str(instrument["instrument_id"])
    latest = fetch_latest_eod(base_url, instrument_id, api_key=api_key)
    if latest is not None:
        return latest

    market = instrument.get("market")
    symbol = instrument.get("symbol")
    if market and symbol:
        return fetch_latest_futures_continuous(
            base_url,
            str(market),
            str(symbol),
            api_key=api_key,
        )

    return None


def fetch_macro_series_page(
    base_url: str,
    page: int,
    page_size: int,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch a single paginated macro series response from the Serve API."""
    return fetch_page(base_url, "macro/series", page, page_size, api_key)


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


def enrich_instruments_with_latest_prices(
    instruments: list[dict[str, Any]],
    base_url: str,
    api_key: str | None = None,
    max_workers: int = LATEST_PRICE_WORKERS,
) -> list[dict[str, Any]]:
    """Add latest EOD trade date and close price when the instruments API omits them."""
    needs_enrichment = [
        item
        for item in instruments
        if item.get("instrument_id")
        and (item.get("latest_trade_date") is None or item.get("latest_price") is None)
    ]
    if not needs_enrichment:
        return instruments

    enriched_by_id: dict[str, dict[str, Any]] = {}
    worker_count = max(1, min(max_workers, len(needs_enrichment)))

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                fetch_latest_price_snapshot,
                base_url,
                item,
                api_key,
            ): item
            for item in needs_enrichment
        }
        for future in as_completed(futures):
            item = futures[future]
            instrument_id = str(item["instrument_id"])
            latest_eod = future.result()
            enriched = dict(item)
            enriched["latest_trade_date"] = latest_eod.get("trade_date") if latest_eod else None
            enriched["latest_price"] = latest_eod.get("close") if latest_eod else None
            enriched_by_id[instrument_id] = enriched

    return [
        enriched_by_id.get(str(item.get("instrument_id")), item)
        for item in instruments
    ]


def collect_macro_series(
    base_url: str,
    api_key: str | None = None,
    page_size: int = PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Fetch every page from the macro series Serve API."""
    collected: list[dict[str, Any]] = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        payload = fetch_macro_series_page(
            base_url,
            page=page,
            page_size=page_size,
            api_key=api_key,
        )
        if payload.get("success") is not True:
            raise RuntimeError(f"Macro series API returned unsuccessful payload on page {page}")

        page_items = payload.get("data")
        pagination = payload.get("pagination") or {}
        if not isinstance(page_items, list):
            raise RuntimeError(f"Unexpected macro response format on page {page}: missing data array")

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


def print_macro_summary(payload: dict[str, Any]) -> None:
    """Print a concise macro cache summary to stdout."""
    counts = Counter(item.get("market") or "UNKNOWN" for item in payload["data"])
    print(
        "Macro series cache generated:",
        f"total={payload['total']}",
        f"output={MACRO_OUTPUT_PATH}",
    )
    for market, count in sorted(counts.items()):
        print(f"  {market}: {count}")


def main() -> int:
    """CLI entry point."""
    try:
        instruments = collect_instruments(BASE_URL, api_key=SERVE_API_KEY or None)
        instruments = enrich_instruments_with_latest_prices(
            instruments,
            BASE_URL,
            api_key=SERVE_API_KEY or None,
        )
        payload = build_cache_payload(instruments)
        macro_series = collect_macro_series(BASE_URL, api_key=SERVE_API_KEY or None)
        macro_payload = build_macro_series_payload(macro_series)

        write_json_atomic(OUTPUT_PATH, payload)
        print_summary(payload)
        write_json_atomic(MACRO_OUTPUT_PATH, macro_payload)
        print_macro_summary(macro_payload)
        return 0
    except Exception as exc:
        print(f"Failed to generate static lookup cache: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
