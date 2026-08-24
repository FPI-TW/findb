"""Canonical scheduler-slot identity.

Slot identifiers are deliberately semantic and time-independent.  The
application contract accepts only the canonical values exported here;
historical aliases are handled by migrations and must not be normalized by
runtime code.
"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

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


def normalize_slot_id(value: Any) -> str:
    """Validate and return a canonical slot ID without rewriting input."""

    normalized = str(value or "").strip()
    if normalized in CANONICAL_SLOT_IDS:
        return normalized
    raise ValueError(f"Unsupported slot_id: {value!r}")


# A descriptive alias for callers that use ``canonical_*`` terminology.
canonical_slot_id = normalize_slot_id


__all__ = [
    "CANONICAL_SLOT_IDS",
    "CanonicalSlotId",
    "canonical_slot_id",
    "normalize_slot_id",
]
