"""
Taiwan stock MultiCharts direct-format normalizer.
"""

from app.services.normalize.base import BaseNormalizer
from app.services.normalize.types import MappedRecord

_TWSTOCK_ALLOWED_ASSET_CLASSES = frozenset({"equity", "index", "future"})


class TWStockMultichartsNormalizer(BaseNormalizer):
    """Normalizer for TW stock MultiCharts direct ingest payloads.

    By default rows are classified as ``asset_class="equity"``. To ingest
    TAIFEX index/futures contracts via the same endpoint without misclassifying
    them, set ``metadata.asset_class`` to ``"index"`` or ``"future"`` (validated
    upstream by ``TWStockDirectMetadata``; this normalizer re-validates as a
    safety net since rerun paths bypass the schema).
    """

    dataset_key = "tw_equity_multicharts_eod"
    asset_class = "equity"
    market = "TW"

    def _resolve_asset_class(self, metadata: dict) -> str:
        override = metadata.get("asset_class")
        if override is None:
            return self.asset_class
        if not isinstance(override, str):
            return self.asset_class
        normalized = override.strip().lower()
        if normalized in _TWSTOCK_ALLOWED_ASSET_CLASSES:
            return normalized
        return self.asset_class

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        """Map TW MultiCharts direct payload to canonical EOD records."""
        metadata = raw_data.get("metadata", {}) or {}
        data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        base_symbol = str(metadata.get("symbol") or "").strip().upper()
        base_name = metadata.get("name")
        source_value = str(metadata.get("source") or "multicharts").strip().lower()
        source = source_value or "multicharts"
        asset_class = self._resolve_asset_class(metadata)

        records: list[MappedRecord] = []
        for item in data_items:
            if not isinstance(item, dict):
                continue

            trade_date = self._parse_trade_date(item.get("date"))
            if trade_date is None:
                continue

            symbol = str(item.get("symbol") or base_symbol or "").strip().upper()
            if not symbol:
                continue

            total_volume = item.get("total_volume")
            volume_value = total_volume if total_volume is not None else item.get("volume")
            record = MappedRecord(
                symbol=symbol,
                trade_date=trade_date,
                market="TW",
                asset_class=asset_class,
                name=item.get("name") or base_name,
                open=self._parse_decimal(item.get("open")),
                high=self._parse_decimal(item.get("high")),
                low=self._parse_decimal(item.get("low")),
                close=self._parse_decimal(item.get("close")),
                volume=self._parse_int(volume_value),
                up_volume=self._parse_int(item.get("up_volume")),
                down_volume=self._parse_int(item.get("down_volume")),
                up_ticks=self._parse_int(item.get("up_ticks")),
                down_ticks=self._parse_int(item.get("down_ticks")),
                total_ticks=self._parse_int(item.get("total_ticks")),
                source=source,
                raw_data=item,
            )
            records.append(record)

        return records
