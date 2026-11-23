"""Parse the instrument references Serve accepts from callers.

A caller names an instrument either by its FinDB UUID or as ``MARKET:SYMBOL``
(``TW:2330``, ``US:AAPL``, ``HK:700``).  Symbols are normalized the way FinDB
stores them, so an agent can pass what a user typed.
"""

import unicodedata
from dataclasses import dataclass
from uuid import UUID

from app.services.serve_errors import serve_error

MAX_SELECTORS = 50
# HKEX stock codes are stored zero-padded to five digits, as HKEX publishes them.
HK_CODE_WIDTH = 5


@dataclass(frozen=True)
class InstrumentSelector:
    """One requested instrument: ``instrument_id`` or ``(market, symbol)``."""

    input: str
    instrument_id: UUID | None = None
    market: str | None = None
    symbol: str | None = None


def normalize_market(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().upper()


def normalize_symbol(market: str, value: str) -> str:
    symbol = unicodedata.normalize("NFKC", value).strip().upper()
    if market == "HK" and symbol.isdigit() and len(symbol) <= HK_CODE_WIDTH:
        symbol = symbol.zfill(HK_CODE_WIDTH)
    return symbol


def parse_selectors(
    raw: str,
    *,
    param: str,
    default_market: str | None = None,
    allow_uuid: bool = False,
) -> list[InstrumentSelector]:
    """Split a comma-separated list; duplicates collapse to their first occurrence."""
    tokens = [token.strip() for token in raw.split(",")]
    if not 1 <= len(tokens) <= MAX_SELECTORS or any(not token for token in tokens):
        raise serve_error(
            422,
            "INVALID_SYMBOLS",
            f"{param} must hold 1 to {MAX_SELECTORS} comma-separated values",
            details={"param": param},
        )
    market_default = normalize_market(default_market) if default_market else None
    selectors: list[InstrumentSelector] = []
    seen: set[tuple[UUID | None, str | None, str | None]] = set()
    for token in tokens:
        selector = _parse_one(token, market_default, allow_uuid=allow_uuid, param=param)
        identity = (selector.instrument_id, selector.market, selector.symbol)
        if identity not in seen:
            seen.add(identity)
            selectors.append(selector)
    return selectors


def _parse_one(
    token: str, default_market: str | None, *, allow_uuid: bool, param: str
) -> InstrumentSelector:
    if allow_uuid:
        try:
            return InstrumentSelector(input=token, instrument_id=UUID(token))
        except ValueError:
            pass
    market, separator, symbol = token.partition(":")
    if not separator:
        market, symbol = default_market or "", token
    market = normalize_market(market)
    if not market or not symbol.strip():
        expected = "a FinDB UUID or MARKET:SYMBOL" if allow_uuid else "MARKET:SYMBOL"
        raise serve_error(
            422,
            "INVALID_SYMBOLS",
            f"{token!r} in {param} is not {expected}, e.g. TW:2330",
            details={"param": param, "value": token},
        )
    return InstrumentSelector(input=token, market=market, symbol=normalize_symbol(market, symbol))
