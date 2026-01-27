"""
Foreign Exchange (FX) data normalizer.
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
