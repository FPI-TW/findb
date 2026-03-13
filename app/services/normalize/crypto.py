"""
Cryptocurrency data normalizer.
"""

from typing import Optional

from app.services.normalize.base import BaseNormalizer
from app.services.normalize.types import MappedRecord
from app.services.normalize.usstock import USStockNormalizer


class CryptoNormalizer(BaseNormalizer):
    """
    Normalizer for cryptocurrency market data.
    Handles Bloomberg crypto data format.
    """

    dataset_key = "crypto_eod"
    asset_class = "crypto"
    market = "CRYPTO"

    # Bloomberg ticker to symbol mapping
    TICKER_MAP = {
        "XBTUSD BGN Curncy": "BTC",
        "XETUSD BGN Curncy": "ETH",
        "XRP Curncy": "XRP",
        "XSO Curncy": "SOL",
        "XAD BGN Curncy": "ADA",
    }

    # Name mapping for instruments
    NAME_MAP = {
        "BTC": "Bitcoin",
        "ETH": "Ethereum",
        "XRP": "Ripple",
        "SOL": "Solana",
        "ADA": "Cardano",
    }

    DEFAULT_CONFIG = {
        "field_mapping": {
            "open": "price.open",
            "high": "price.high",
            "low": "price.low",
            "close": "price.last",
            "trade_date": "timestamp.last_update",
        },
        "identifier_type": "bloomberg",
        "identifier_field": "ticker",
    }

    def _resolve_symbol(self, ticker: str) -> str:
        """Resolve Bloomberg ticker to standard symbol."""
        return self.TICKER_MAP.get(ticker, ticker)

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        """
        Map Bloomberg crypto data to canonical format.

        Expected raw_data structure (based on actual API format):
        {
            "metadata": {
                "source": "Bloomberg API",
                "category": "Cryptocurrency",
                "query_time": "2026-01-16T14:49:14.910965",
                "total_records": 5
            },
            "data": [
                {
                    "crypto_id": "bitcoin",
                    "symbol": "BTC",
                    "name": "Bitcoin",
                    "ticker": "XBTUSD BGN Curncy",
                    "price": {
                        "last": 95709.01,
                        "open": 95550.07,
                        "high": 95825.34,
                        "low": 95119.76
                    },
                    "change": {...},
                    "timestamp": {
                        "query_time": "...",
                        "last_update": "2026-01-16"
                    },
                    "metadata": {...}
                },
                ...
            ]
        }
        """
        config = getattr(self, "dataset_config", None) or self.DEFAULT_CONFIG
        records = self.map_fields_from_config(raw_data, config_override=config)
        normalized: list[MappedRecord] = []

        for record in records:
            if record.symbol:
                record.symbol = self._resolve_symbol(record.symbol)
            elif record.identifier_value:
                record.symbol = self._resolve_symbol(record.identifier_value)

            if not record.symbol:
                continue

            if not record.name:
                record.name = self.NAME_MAP.get(record.symbol)

            if record.source == "Bloomberg API":
                record.source = "bloomberg"
            if not record.source:
                record.source = "bloomberg"

            if record.identifier_value and not record.identifier_type:
                record.identifier_type = "bloomberg"

            normalized.append(record)

        return normalized

    async def get_or_create_instrument(
        self,
        symbol: str,
        name: Optional[str] = None,
        market: Optional[str] = None,
        asset_class: Optional[str] = None,
    ):
        """Get existing instrument or create new one with crypto-specific defaults."""
        # Use name from mapping if not provided
        if name is None:
            name = self.NAME_MAP.get(symbol)

        return await super().get_or_create_instrument(symbol, name, market=market, asset_class=asset_class)


class CryptoBloombergNormalizer(USStockNormalizer):
    """
    Normalizer for cryptocurrency data from Bloomberg API direct format.

    Same payload structure as USStockNormalizer but resolves Bloomberg crypto
    tickers (e.g. "XBTUSD BGN Curncy") to standard symbols (e.g. "BTC").

    Expected data format:
    {
        "metadata": {"source": "Bloomberg API", "category": "Cryptocurrency"},
        "data": [
            {
                "symbol": "BTC",
                "name": "Bitcoin",
                "ticker": "XBTUSD BGN Curncy",
                "price": {"last": 95709.01, "open": 95550.07, "high": 95825.34, "low": 95119.76},
                "timestamp": {"query_time": "...", "last_update": "2026-01-16"},
                "metadata": {"source": "Bloomberg"}
            }
        ]
    }
    """

    dataset_key = "crypto_bloomberg_eod"
    asset_class = "crypto"
    market = "CRYPTO"

    TICKER_MAP = {
        "XBTUSD BGN Curncy": "BTC",
        "XETUSD BGN Curncy": "ETH",
        "XRP Curncy": "XRP",
        "XSO Curncy": "SOL",
        "XAD BGN Curncy": "ADA",
    }

    NAME_MAP = {
        "BTC": "Bitcoin",
        "ETH": "Ethereum",
        "XRP": "Ripple",
        "SOL": "Solana",
        "ADA": "Cardano",
    }

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        """Map Bloomberg crypto data to canonical format."""
        all_records = super().map_fields(raw_data)
        normalized: list[MappedRecord] = []

        for record in all_records:
            if record.identifier_value and record.identifier_value in self.TICKER_MAP:
                record.symbol = self.TICKER_MAP[record.identifier_value]
            elif record.symbol:
                record.symbol = self.TICKER_MAP.get(record.symbol, record.symbol)

            if not record.name:
                record.name = self.NAME_MAP.get(record.symbol)

            record.asset_class = "crypto"
            normalized.append(record)

        return normalized
