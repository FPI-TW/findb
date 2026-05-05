# Normalize services
from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.normalize.corporate_actions import CorporateActionNormalizer
from app.services.normalize.crypto import CryptoBloombergNormalizer, CryptoNormalizer
from app.services.normalize.crypto_index import CryptoIndexNormalizer
from app.services.normalize.equity import EquityNormalizer, IndexNormalizer
from app.services.normalize.futures import (
    FuturesContinuousNormalizer,
    FuturesContractNormalizer,
    WTXBloombergNormalizer,
)
from app.services.normalize.fx import FXBloombergNormalizer, FXNormalizer
from app.services.normalize.macro import MacroBloombergNormalizer, MacroNormalizer
from app.services.normalize.twstock import TWStockMultichartsNormalizer
from app.services.normalize.types import (
    CorporateActionRecord,
    FuturesContinuousRecord,
    FuturesContractRecord,
    MacroObservationRecord,
    MappedRecord,
)
from app.services.normalize.usstock import (
    CNEquityNormalizer,
    CNIndexNormalizer,
    GlobalStockNormalizer,
    HKChinaIndexNormalizer,
    HKChinaMixedNormalizer,
    HKEquityNormalizer,
    HKIndexNormalizer,
    TWEquityNormalizer,
    TWIndexNormalizer,
    USIndexNormalizer,
    USStockNormalizer,
)

__all__ = [
    "BaseNormalizer",
    "NormalizeResult",
    "MappedRecord",
    "CryptoNormalizer",
    "CryptoBloombergNormalizer",
    "CryptoIndexNormalizer",
    "EquityNormalizer",
    "IndexNormalizer",
    "FXNormalizer",
    "FXBloombergNormalizer",
    "CorporateActionNormalizer",
    "MacroNormalizer",
    "MacroBloombergNormalizer",
    "FuturesContractNormalizer",
    "FuturesContinuousNormalizer",
    "WTXBloombergNormalizer",
    "USStockNormalizer",
    "USIndexNormalizer",
    "GlobalStockNormalizer",
    "TWEquityNormalizer",
    "HKEquityNormalizer",
    "CNEquityNormalizer",
    "TWIndexNormalizer",
    "HKIndexNormalizer",
    "CNIndexNormalizer",
    "HKChinaMixedNormalizer",
    "HKChinaIndexNormalizer",
    "TWStockMultichartsNormalizer",
    "CorporateActionRecord",
    "MacroObservationRecord",
    "FuturesContractRecord",
    "FuturesContinuousRecord",
]
