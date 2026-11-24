"""Normalizers for supported versioned ingress schemas."""

from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.normalize.contracts import (
    FuturesEODContractNormalizer,
    MarketEODContractNormalizer,
    MarketEODV2ContractNormalizer,
    MarketMinuteContractNormalizer,
)
from app.services.normalize.exchange_quote import ExchangeDailyQuoteNormalizer
from app.services.normalize.index_reference import (
    IndexChangesNormalizer,
    IndexConstituentsNormalizer,
)
from app.services.normalize.institutional_flow import (
    InstitutionalFlowStockNormalizer,
    InstitutionalFlowSummaryNormalizer,
)
from app.services.normalize.intraday import IntradayPartitionContractNormalizer
from app.services.normalize.types import MappedRecord, MarketMinuteRecord

__all__ = [
    "BaseNormalizer",
    "NormalizeResult",
    "MappedRecord",
    "MarketMinuteRecord",
    "MarketEODContractNormalizer",
    "MarketEODV2ContractNormalizer",
    "MarketMinuteContractNormalizer",
    "FuturesEODContractNormalizer",
    "IntradayPartitionContractNormalizer",
    "ExchangeDailyQuoteNormalizer",
    "InstitutionalFlowSummaryNormalizer",
    "InstitutionalFlowStockNormalizer",
    "IndexConstituentsNormalizer",
    "IndexChangesNormalizer",
]
