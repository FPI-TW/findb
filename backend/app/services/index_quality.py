"""Index fields FinDB stores but cannot vouch for.

FMP's index rows were compared with Yahoo for a month (2026-10-20 to 11-24): every
close agreed, but for these indices the open, high and low disagreed on 13 to 21 of
25 days. They have no reliable official open: each vendor fills it (Yahoo mostly
with the previous close), and the high or low then follows the filled open. Their
rows are still written as served, with a DQ warning; only the close is reliable.
"""

from __future__ import annotations

# (market, symbol) of indices whose open, high and low are vendor fills.
CLOSE_ONLY_INDICES: frozenset[tuple[str, str]] = frozenset(
    ("US", symbol) for symbol in ("^NYA", "^RUT", "^RUA", "^RLG", "^RLV", "^SPXEW", "^PSE", "^XAX")
)


def close_only(market: str | None, symbol: str | None) -> bool:
    """Whether only this index's close can be relied on."""
    if not market or not symbol:
        return False
    return (market.strip().upper(), symbol.strip().upper()) in CLOSE_ONLY_INDICES


# Sources whose index volume is preliminary when first published: FMP serves 0 for
# hours after the close (^HSI, ^HSCE, ^NYA, ^RUT) and fills it the next day, and a
# version served while its rows settle may show 0 again (#680). A 0 from such a
# source is therefore never evidence that a stored, non-zero volume was wrong.
PRELIMINARY_ZERO_VOLUME_SOURCES: frozenset[str] = frozenset({"fmp"})


def zero_volume_keeps_stored(source: str | None, asset_class: str | None) -> bool:
    """Whether a 0 volume from this source must not replace a stored non-zero one."""
    return (asset_class or "").strip().lower() == "index" and (
        source or ""
    ).strip().lower() in PRELIMINARY_ZERO_VOLUME_SOURCES
