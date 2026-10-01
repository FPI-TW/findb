"""Normalizers for supported versioned ingress schemas."""

from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.normalize.contracts import (
    FuturesEODContractNormalizer,
    MarketEODContractNormalizer,
    MarketMinuteContractNormalizer,
)
from app.services.normalize.types import MappedRecord, MarketMinuteRecord

__all__ = [
    "BaseNormalizer",
    "NormalizeResult",
    "MappedRecord",
    "MarketMinuteRecord",
    "MarketEODContractNormalizer",
    "MarketMinuteContractNormalizer",
    "FuturesEODContractNormalizer",
]
