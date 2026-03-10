"""
US Stock data normalizer for Bloomberg API format.
"""

from app.services.normalize.base import BaseNormalizer
from app.services.normalize.types import MappedRecord


def _merge_config(default: dict, override: dict | None) -> dict:
    """Merge dataset config with defaults."""
    if not override:
        return {**default}

    merged = {**default, **override}
    if "field_mapping" in override:
        merged["field_mapping"] = override["field_mapping"]
    return merged


class USStockNormalizer(BaseNormalizer):
    """
    Normalizer for US Stock market data from Bloomberg API.

    Expected data format:
    {
        "metadata": {
            "source": "Bloomberg API",
            "category": "US Stock",
            "query_time": "2026-02-04T16:00:51.164789",
            "total_records": 63
        },
        "data": [
            {
                "stock_id": "aapl",
                "symbol": "AAPL",
                "name": "APPLE INC",
                "ticker": "AAPL US Equity",
                "price": {
                    "last": 269.48,
                    "open": 269.2,
                    "high": 271.875,
                    "low": 267.61,
                    "volume": 64394655.0
                },
                "change": {
                    "percent_1d": -0.196289,
                    "percent_5d": 4.340419,
                    "percent_1m": -0.5645584,
                    "percent_ytd": -0.8754453
                },
                "market_cap": {
                    "value": 3956273.6072,
                    "currency": "USD"
                },
                "timestamp": {
                    "query_time": "2026-02-04T16:00:51.151296",
                    "last_update": null
                },
                "metadata": {
                    "source": "Bloomberg",
                    "data_type": "stock"
                }
            }
        ]
    }
    """

    dataset_key = "us_stock_eod"
    asset_class = "equity"
    market = "US"

    DEFAULT_CONFIG = {
        "data_path": "data",
        "symbol_field": "symbol",
        "name_field": "name",
        "source_field": "metadata.source",
        "identifier_field": "ticker",
        "identifier_type": "bloomberg",
        "field_mapping": {
            "trade_date": "timestamp.query_time",  # Use query_time as fallback
            "open": "price.open",
            "high": "price.high",
            "low": "price.low",
            "close": "price.last",
            "volume": "price.volume",
        },
    }

    # Map market suffix to market code
    MARKET_MAP = {
        "US": "US",
        "GY": "DE",  # Germany
        "HK": "HK",  # Hong Kong
        "TT": "TW",  # Taiwan
        "IN": "IN",  # India
        "JP": "JP",  # Japan
        "CH": "CN",  # China
        "SS": "SE",  # Sweden
        "GR": "DE",  # Germany (Xetra)
    }

    # Index tickers that should be classified as index instead of equity
    INDEX_TICKERS = {
        "SPX Index",
        "NDX INDEX",
        "INDU INDEX",
        "SOX INDEX",
        "RTY INDEX",
        "VIX Index",
        "S5INFT Index",
        "S5COND Index",
        "S5TELS Index",
        "S5RLST Index",
        "S5INDU Index",
        "S5MATR Index",
        "S5FINL Index",
        "S5HLTH Index",
        "S5ENRS Index",
        "S5UTIL Index",
        "S5CONS Index",
        "BM7T Index",
    }

    def _is_index(self, ticker: str | None) -> bool:
        """Check if ticker represents an index."""
        if not ticker:
            return False
        return ticker in self.INDEX_TICKERS or ticker.upper().endswith(" INDEX")

    def _extract_market_from_ticker(self, ticker: str | None) -> str:
        """Extract market code from Bloomberg ticker."""
        if not ticker:
            return self.market

        parts = ticker.split()
        if len(parts) >= 2:
            market_suffix = parts[-2].upper() if parts[-1].upper() in ("EQUITY", "INDEX") else None
            if market_suffix and market_suffix in self.MARKET_MAP:
                return self.MARKET_MAP[market_suffix]

        return self.market

    def _parse_trade_date_from_item(self, item: dict) -> str | None:
        """
        Get trade date from item, preferring last_update over query_time.
        Falls back to flat 'date' field for direct-format payloads.
        """
        timestamp = item.get("timestamp", {})
        last_update = timestamp.get("last_update")
        if last_update:
            return last_update

        query_time = timestamp.get("query_time")
        if query_time:
            return query_time

        return item.get("date")

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        """
        Map Bloomberg US Stock data to canonical format.
        """
        config = _merge_config(self.DEFAULT_CONFIG, getattr(self, "dataset_config", None))

        data_path = config.get("data_path", "data")
        data_items = self._get_nested_value(raw_data, data_path) if data_path else None
        if data_items is None:
            data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        normalized: list[MappedRecord] = []

        for item in data_items:
            # Get trade date
            trade_date_str = self._parse_trade_date_from_item(item)
            trade_date = self._parse_trade_date(trade_date_str)
            if trade_date is None:
                continue

            # Extract basic fields
            symbol = item.get("symbol")
            name = item.get("name")
            ticker = item.get("ticker")

            if not symbol and not ticker:
                continue

            # If symbol is missing, derive from ticker
            if not symbol and ticker:
                symbol = ticker.split()[0].upper()

            # Get price data — support both nested {"price": {...}} and flat fields
            price = item.get("price") or {}
            open_price = self._parse_decimal(price.get("open") or item.get("open"))
            high_price = self._parse_decimal(price.get("high") or item.get("high"))
            low_price = self._parse_decimal(price.get("low") or item.get("low"))
            close_price = self._parse_decimal(price.get("last") or item.get("close"))
            volume = self._parse_int(price.get("volume") or item.get("volume"))

            # Get source
            item_metadata = item.get("metadata", {})
            source = item_metadata.get("source") or raw_data.get("metadata", {}).get("source")
            if source:
                source_lower = source.lower()
                source = "bloomberg" if source_lower.startswith("bloomberg") else source_lower
            else:
                source = "bloomberg"

            record = MappedRecord(
                symbol=symbol.upper() if symbol else "",
                trade_date=trade_date,
                asset_class="index" if self._is_index(ticker) else "equity",
                name=name,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                source=source,
                raw_data=item,
                identifier_type="bloomberg" if ticker else None,
                identifier_value=ticker,
            )

            normalized.append(record)

        return normalized


class USIndexNormalizer(USStockNormalizer):
    """
    Normalizer for US Index data from Bloomberg API.
    Extends USStockNormalizer but specifically for index data.
    """

    dataset_key = "us_index_eod"
    asset_class = "index"
    market = "US"

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        """
        Map Bloomberg US Index data to canonical format.
        Only processes records that are identified as indices.
        """
        all_records = super().map_fields(raw_data)

        # Filter to only include index records
        return [record for record in all_records if self._is_index(record.identifier_value)]


class RegionalIndexNormalizer(USIndexNormalizer):
    """Base normalizer for regional stock-market indices."""

    dataset_key = "regional_index_eod"
    market = "GLOBAL"

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        """Map and keep only indices that belong to the configured regional market."""
        index_records = super().map_fields(raw_data)
        normalized: list[MappedRecord] = []

        for record in index_records:
            ticker = record.identifier_value
            resolved_market = self._extract_market_from_ticker(ticker)

            if resolved_market == self.market:
                record.market = self.market
                normalized.append(record)
                continue

            # Fallback: some payloads provide market code explicitly.
            raw_market = None
            if isinstance(record.raw_data, dict):
                raw_market = record.raw_data.get("market")
            if raw_market:
                raw_market_code = str(raw_market).upper().strip()
                fallback_market = self.MARKET_MAP.get(raw_market_code, raw_market_code)
                if fallback_market == self.market:
                    record.market = fallback_market
                    normalized.append(record)

        return normalized


class TWIndexNormalizer(RegionalIndexNormalizer):
    """Normalizer for Taiwan stock-market indices."""

    dataset_key = "tw_index_eod"
    market = "TW"


class HKIndexNormalizer(RegionalIndexNormalizer):
    """Normalizer for Hong Kong stock-market indices."""

    dataset_key = "hk_index_eod"
    market = "HK"


class CNIndexNormalizer(RegionalIndexNormalizer):
    """Normalizer for China stock-market indices."""

    dataset_key = "cn_index_eod"
    market = "CN"


class GlobalStockNormalizer(USStockNormalizer):
    """
    Normalizer for global stock data from Bloomberg API.
    Handles stocks from multiple markets (US, DE, TW, JP, CN, etc.)
    """

    dataset_key = "global_stock_eod"
    asset_class = "equity"
    market = "GLOBAL"

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        """
        Map Bloomberg global stock data to canonical format.
        Dynamically sets market based on ticker suffix.
        """
        config = _merge_config(self.DEFAULT_CONFIG, getattr(self, "dataset_config", None))

        data_path = config.get("data_path", "data")
        data_items = self._get_nested_value(raw_data, data_path) if data_path else None
        if data_items is None:
            data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        normalized: list[MappedRecord] = []

        for item in data_items:
            # Skip index records
            ticker = item.get("ticker")
            if self._is_index(ticker):
                continue

            # Resolve market by ticker suffix, fallback to item.market
            market = self._extract_market_from_ticker(ticker)
            if market == self.market:
                item_market = item.get("market")
                if item_market:
                    item_market_code = str(item_market).upper().strip()
                    market = self.MARKET_MAP.get(item_market_code, item_market_code)

            # Get trade date
            trade_date_str = self._parse_trade_date_from_item(item)
            trade_date = self._parse_trade_date(trade_date_str)
            if trade_date is None:
                continue

            # Extract basic fields
            symbol = item.get("symbol")
            name = item.get("name")

            if not symbol and not ticker:
                continue

            # If symbol is missing, derive from ticker
            if not symbol and ticker:
                symbol = ticker.split()[0].upper()

            # Get price data — support both nested {"price": {...}} and flat fields
            price = item.get("price") or {}
            open_price = self._parse_decimal(price.get("open") or item.get("open"))
            high_price = self._parse_decimal(price.get("high") or item.get("high"))
            low_price = self._parse_decimal(price.get("low") or item.get("low"))
            close_price = self._parse_decimal(price.get("last") or item.get("close"))
            volume = self._parse_int(price.get("volume") or item.get("volume"))

            # Get source
            item_metadata = item.get("metadata", {})
            source = item_metadata.get("source") or raw_data.get("metadata", {}).get("source")
            if source:
                source_lower = source.lower()
                source = "bloomberg" if source_lower.startswith("bloomberg") else source_lower
            else:
                source = "bloomberg"

            record = MappedRecord(
                symbol=symbol.upper() if symbol else "",
                trade_date=trade_date,
                market=market,
                name=name,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                source=source,
                raw_data=item,
                identifier_type="bloomberg" if ticker else None,
                identifier_value=ticker,
            )

            normalized.append(record)

        return normalized


class RegionalEquityNormalizer(GlobalStockNormalizer):
    dataset_key = "regional_equity_eod"
    market = "GLOBAL"

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        records = super().map_fields(raw_data)
        return [record for record in records if record.market == self.market]


class TWEquityNormalizer(RegionalEquityNormalizer):
    dataset_key = "tw_equity_eod"
    market = "TW"


class HKEquityNormalizer(RegionalEquityNormalizer):
    dataset_key = "hk_equity_eod"
    market = "HK"


class CNEquityNormalizer(RegionalEquityNormalizer):
    dataset_key = "cn_equity_eod"
    market = "CN"
