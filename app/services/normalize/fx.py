"""
Foreign Exchange (FX) data normalizer.
"""

from app.services.normalize.base import BaseNormalizer
from app.services.normalize.types import MappedRecord
from app.services.normalize.usstock import USStockNormalizer


def _merge_config(default: dict, override: dict | None) -> dict:
    """Merge dataset config with defaults."""
    if not override:
        return {**default}

    merged = {**default, **override}
    if "field_mapping" in override:
        merged["field_mapping"] = override["field_mapping"]
    return merged


class FXNormalizer(BaseNormalizer):
    """
    Normalizer for foreign exchange market data.
    """

    dataset_key = "fx_eod"
    asset_class = "fx"
    market = "FX"

    DEFAULT_CONFIG = {
        "data_path": "data",
        "symbol_field": "pair",
        "name_field": "name",
        "source_field": "metadata.source",
        "identifier_field": "ticker",
        "identifier_type": "bloomberg",
        "field_mapping": {
            "trade_date": "timestamp.last_update",
            "open": "price.open",
            "high": "price.high",
            "low": "price.low",
            "close": "price.last",
            "volume": "volume",
            "turnover": "turnover",
        },
    }

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        """
        Map FX data to canonical format.

        Expects API-format payload with price/timestamp fields.
        """
        config = _merge_config(self.DEFAULT_CONFIG, getattr(self, "dataset_config", None))
        records = self.map_fields_from_config(raw_data, config_override=config)
        normalized: list[MappedRecord] = []

        for record in records:
            if not record.symbol and record.raw_data:
                raw_symbol = record.raw_data.get("symbol") or record.raw_data.get("pair")
                if raw_symbol:
                    record.symbol = str(raw_symbol)

            if record.symbol:
                record.symbol = record.symbol.upper().strip()
            elif record.identifier_value:
                record.symbol = str(record.identifier_value).split(" ")[0].upper()

            if record.identifier_value and not record.identifier_type:
                record.identifier_type = "bloomberg"

            if record.source:
                source_value = record.source.lower()
                record.source = (
                    "bloomberg" if source_value.startswith("bloomberg") else source_value
                )
            else:
                record.source = "bloomberg"

            normalized.append(record)

        return normalized


class FXBloombergNormalizer(USStockNormalizer):
    """
    Normalizer for FX data from Bloomberg API direct format.

    Expected data format:
    {
        "metadata": {"source": "Bloomberg API", "category": "FX"},
        "data": [
            {
                "pair": "EURUSD",
                "symbol": "EURUSD",
                "ticker": "EURUSD Curncy",
                "price": {"last": 1.0523, "open": 1.0498, "high": 1.0567, "low": 1.0489},
                "timestamp": {"query_time": "...", "last_update": "2026-03-12"},
                "metadata": {"source": "Bloomberg"}
            }
        ]
    }
    """

    dataset_key = "fx_bloomberg_eod"
    asset_class = "fx"
    market = "FX"

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        """Map Bloomberg FX data to canonical format."""
        config = _merge_config(self.DEFAULT_CONFIG, getattr(self, "dataset_config", None))

        data_path = config.get("data_path", "data")
        data_items = self._get_nested_value(raw_data, data_path) if data_path else None
        if data_items is None:
            data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        normalized: list[MappedRecord] = []

        for item in data_items:
            trade_date_str = self._parse_trade_date_from_item(item)
            trade_date = self._parse_trade_date(trade_date_str)
            if trade_date is None:
                continue

            symbol = item.get("symbol") or item.get("pair")
            name = item.get("name")
            ticker = item.get("ticker")

            if not symbol and ticker:
                symbol = ticker.split()[0].upper()

            if not symbol and not ticker:
                continue

            price = item.get("price") or {}
            open_price = self._parse_decimal(price.get("open") or item.get("open"))
            high_price = self._parse_decimal(price.get("high") or item.get("high"))
            low_price = self._parse_decimal(price.get("low") or item.get("low"))
            close_price = self._parse_decimal(price.get("last") or item.get("close"))
            volume = self._parse_int(price.get("volume") or item.get("volume"))

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
                asset_class="fx",
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
