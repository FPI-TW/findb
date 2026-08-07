"""Canonical scheduler-slot identity and bounded rollout compatibility.

Slot identifiers are deliberately semantic and time-independent.  The legacy
identifiers remain accepted only at ingress/configuration compatibility
boundaries; callers producing new payloads should use the canonical values
exported here.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Literal, Mapping, TypeAlias

CanonicalSlotId: TypeAlias = Literal[
    "western_markets_window",
    "global_markets_window",
    "taiwan_market_window",
    "asia_pacific_markets_window",
]

CANONICAL_SLOT_IDS = (
    "western_markets_window",
    "global_markets_window",
    "taiwan_market_window",
    "asia_pacific_markets_window",
)

LEGACY_SLOT_ID_MAP = MappingProxyType(
    {
        "us_0600": "western_markets_window",
        "global_0815": "global_markets_window",
        "tw_1430": "taiwan_market_window",
        "asia_1630": "asia_pacific_markets_window",
    }
)

# Alias name used by a few integrations which describe this as a compatibility
# map rather than a legacy-only map.
LEGACY_TO_CANONICAL_SLOT_ID = LEGACY_SLOT_ID_MAP


def normalize_slot_id(value: Any) -> str:
    """Return a canonical slot ID, accepting only the bounded legacy aliases."""

    normalized = str(value or "").strip()
    if normalized in CANONICAL_SLOT_IDS:
        return normalized
    try:
        return LEGACY_SLOT_ID_MAP[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported slot_id: {value!r}") from exc


# A descriptive alias for callers that use ``canonical_*`` terminology.
canonical_slot_id = normalize_slot_id


def normalize_slot_payload(value: Any) -> Any:
    """Normalize a schedule-like mapping at a compatibility boundary.

    The old ID is the only thing normalized here.  Scheduled time is an
    independent configuration value and must never be inferred from an ID.
    """

    if not isinstance(value, Mapping) or "slot_id" not in value:
        return value

    raw_slot = value.get("slot_id")
    canonical = normalize_slot_id(raw_slot)
    normalized = dict(value)
    normalized["slot_id"] = canonical
    return normalized


__all__ = [
    "CANONICAL_SLOT_IDS",
    "LEGACY_SLOT_ID_MAP",
    "LEGACY_TO_CANONICAL_SLOT_ID",
    "CanonicalSlotId",
    "canonical_slot_id",
    "normalize_slot_id",
    "normalize_slot_payload",
]
