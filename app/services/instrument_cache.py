"""
Read and update the static instrument lookup cache.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

INSTRUMENT_CACHE_PATH = Path(__file__).resolve().parents[1] / "static" / "data" / "instruments.json"

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


class InstrumentCacheNotFoundError(FileNotFoundError):
    """Raised when the cache file has not been generated yet."""


class InstrumentCacheValidationError(ValueError):
    """Raised when the cache payload does not match the expected shape."""


class InstrumentCacheItemNotFoundError(LookupError):
    """Raised when a requested instrument is not present in the cache."""


def read_instrument_cache() -> dict[str, Any]:
    """Read and validate the generated instrument cache JSON."""
    if not INSTRUMENT_CACHE_PATH.exists():
        raise InstrumentCacheNotFoundError(f"Instrument cache not found at {INSTRUMENT_CACHE_PATH}")

    try:
        with INSTRUMENT_CACHE_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise InstrumentCacheValidationError("Instrument cache JSON is invalid") from exc

    return validate_instrument_cache_payload(payload)


def replace_instrument_cache(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace the whole cache file with a normalized payload."""
    normalized = normalize_instrument_cache_payload(payload)
    write_instrument_cache(normalized)
    return normalized


def update_instrument_cache_item(
    instrument_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Patch a single cached instrument by instrument_id."""
    if not updates:
        raise InstrumentCacheValidationError("No instrument fields provided")

    payload = read_instrument_cache()
    updated_item: dict[str, Any] | None = None

    for item in payload["data"]:
        if item.get("instrument_id") == instrument_id:
            item.update(updates)
            updated_item = item
            break

    if updated_item is None:
        raise InstrumentCacheItemNotFoundError(f"Instrument {instrument_id} not found in cache")

    normalized = normalize_instrument_cache_payload(payload)
    write_instrument_cache(normalized)

    for item in normalized["data"]:
        if item.get("instrument_id") == instrument_id:
            return item

    raise InstrumentCacheItemNotFoundError(f"Instrument {instrument_id} not found in cache")


def validate_instrument_cache_payload(payload: Any) -> dict[str, Any]:
    """Validate an existing cache payload without changing its metadata."""
    if not isinstance(payload, dict):
        raise InstrumentCacheValidationError("Instrument cache payload must be an object")

    required_fields = {"generated_at", "total", "markets", "asset_classes", "data"}
    missing_fields = required_fields - set(payload)
    if missing_fields:
        missing = ", ".join(sorted(missing_fields))
        raise InstrumentCacheValidationError(f"Instrument cache missing fields: {missing}")

    normalized = normalize_instrument_cache_payload(
        payload,
        generated_at=str(payload["generated_at"]),
    )
    if payload["total"] != normalized["total"]:
        raise InstrumentCacheValidationError("Instrument cache total does not match data length")

    return payload


def normalize_instrument_cache_payload(
    payload: dict[str, Any],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Normalize cache metadata and ordering before writing to disk."""
    if not isinstance(payload, dict):
        raise InstrumentCacheValidationError("Instrument cache payload must be an object")

    raw_items = payload.get("data")
    if not isinstance(raw_items, list):
        raise InstrumentCacheValidationError("Instrument cache data must be an array")

    items = [_normalize_instrument_item(item) for item in raw_items]
    ordered = sorted(items, key=_sort_key)

    return {
        "generated_at": generated_at or _utc_now_iso(),
        "total": len(ordered),
        "markets": sorted({item["market"] for item in ordered if item.get("market")}),
        "asset_classes": sorted(
            {item["asset_class"] for item in ordered if item.get("asset_class")}
        ),
        "data": ordered,
    }


def write_instrument_cache(payload: dict[str, Any]) -> None:
    """Write JSON to a temp file and atomically replace the target cache."""
    INSTRUMENT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix="instruments.",
        suffix=".tmp",
        dir=INSTRUMENT_CACHE_PATH.parent,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, INSTRUMENT_CACHE_PATH)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def _normalize_instrument_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise InstrumentCacheValidationError("Each instrument cache item must be an object")

    instrument_id = item.get("instrument_id")
    symbol = item.get("symbol")
    if not isinstance(instrument_id, str) or not instrument_id.strip():
        raise InstrumentCacheValidationError("Each instrument must include instrument_id")
    if not isinstance(symbol, str) or not symbol.strip():
        raise InstrumentCacheValidationError("Each instrument must include symbol")

    normalized = {field: item.get(field) for field in INSTRUMENT_FIELDS}
    for field in ("market", "asset_class", "name", "short_name", "currency", "status"):
        value = normalized[field]
        if value is not None and not isinstance(value, str):
            raise InstrumentCacheValidationError(f"Instrument field {field} must be a string")

    latest_trade_date = normalized["latest_trade_date"]
    if latest_trade_date is not None and not isinstance(latest_trade_date, str):
        raise InstrumentCacheValidationError("Instrument field latest_trade_date must be a string")
    latest_price = normalized["latest_price"]
    if latest_price is not None and not isinstance(latest_price, (int, float, str)):
        raise InstrumentCacheValidationError(
            "Instrument field latest_price must be a string or number"
        )

    return normalized


def _sort_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    market = str(item.get("market") or "")
    symbol = str(item.get("symbol") or "")
    name = str(item.get("name") or "")
    short_name = str(item.get("short_name") or "")
    return (market.upper(), symbol.upper(), name.upper(), short_name.upper())


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
