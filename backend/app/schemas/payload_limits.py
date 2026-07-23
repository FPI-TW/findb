"""Shared Source API payload limit validation."""

import json
from typing import Any

from app.config import get_settings


def _current_source_max_payload_bytes() -> int:
    settings = get_settings()
    return max(1, int(settings.SOURCE_MAX_PAYLOAD_BYTES))


def _current_source_max_data_items() -> int:
    settings = get_settings()
    return max(1, int(settings.SOURCE_MAX_DATA_ITEMS))


def ensure_payload_size_within_limit(payload: Any) -> None:
    """Reject payloads whose compact JSON encoding exceeds the configured limit."""
    max_bytes = _current_source_max_payload_bytes()
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    size_bytes = len(serialized.encode("utf-8"))
    if size_bytes > max_bytes:
        raise ValueError(f"Payload exceeds maximum size of {max_bytes} bytes")


def ensure_data_items_count_within_limit(item_count: int) -> None:
    """Reject data arrays that exceed the configured row limit."""
    max_items = _current_source_max_data_items()
    if item_count > max_items:
        raise ValueError(f"Payload data list exceeds maximum size of {max_items} items")
