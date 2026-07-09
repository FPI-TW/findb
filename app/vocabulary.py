"""Canonical vocabulary normalization shared by models and normalizers."""

import re

KNOWN_ASSET_CLASSES = (
    "bond",
    "crypto",
    "equity",
    "etf",
    "future",
    "fx",
    "index",
    "macro",
    "mixed",
)

KNOWN_MARKETS = (
    "CN",
    "CRYPTO",
    "DE",
    "FX",
    "GLOBAL",
    "HK",
    "IN",
    "JP",
    "MACRO",
    "SE",
    "TW",
    "US",
    "WTX",
)

ASSET_CLASS_PATTERN = r"^[a-z][a-z0-9_]{1,19}$"
MARKET_PATTERN = r"^[A-Z][A-Z0-9_]{1,9}$"

_ASSET_CLASS_RE = re.compile(ASSET_CLASS_PATTERN)
_MARKET_RE = re.compile(MARKET_PATTERN)


def sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def sql_asset_class_check(column_name: str) -> str:
    return f"{column_name} ~ '{ASSET_CLASS_PATTERN}'"


def sql_market_check(column_name: str) -> str:
    return f"{column_name} ~ '{MARKET_PATTERN}'"


def normalize_asset_class(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if not _ASSET_CLASS_RE.fullmatch(normalized):
        raise ValueError(f"Unsupported asset_class: {value!r}")
    return normalized


def normalize_market(value: str | None) -> str:
    normalized = str(value or "").strip().upper()
    if not _MARKET_RE.fullmatch(normalized):
        raise ValueError(f"Unsupported market: {value!r}")
    return normalized
