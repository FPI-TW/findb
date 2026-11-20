"""Which markets keep an open or close outside the high/low as a warning."""

from decimal import Decimal

# HKEX's official close is not always a traded price (a non-auction close is the median of
# nominal prices in the last minute), and FactSet's open can sit above the day's high; both
# lie outside the traded high/low on a normal day. Those rows are written with a warning,
# while a high below the low still blocks the write.
OHLC_RANGE_WARNING_MARKETS = frozenset({"HK"})


def ohlc_range_severity(market: str | None, high: Decimal | None, low: Decimal | None) -> str:
    """Return the severity of an open or close outside the high/low of this market."""
    if str(market or "").upper() not in OHLC_RANGE_WARNING_MARKETS:
        return "error"
    return "error" if high is not None and low is not None and high < low else "warning"
