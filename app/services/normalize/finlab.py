"""FinLab direct-format normalizers for Taiwan stocks, ETFs, and TX futures."""

from typing import Optional

from app.services.normalize.base import BaseNormalizer
from app.services.normalize.futures import FuturesContinuousNormalizer
from app.services.normalize.types import FuturesContinuousRecord, MappedRecord

_ASSET_CLASS_ALIAS: dict[str, str] = {
    "stock": "equity",
    "equity": "equity",
    "etf": "etf",
}


def _resolve_asset_class(value: Optional[str], default: str) -> str:
    """Normalize an asset_class hint from FinLab metadata to canonical lowercase form."""
    if value is None:
        return default
    key = str(value).strip().lower()
    if not key:
        return default
    return _ASSET_CLASS_ALIAS.get(key, default)


class TWStockFinlabNormalizer(BaseNormalizer):
    """Normalizer for TW stock FinLab direct ingest payloads (equity)."""

    dataset_key = "tw_equity_eod"
    asset_class = "equity"
    market = "TW"

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        metadata = raw_data.get("metadata", {}) or {}
        data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        base_symbol = str(metadata.get("symbol") or "").strip().upper()
        base_name = metadata.get("name")
        source_value = str(metadata.get("source") or "finlab").strip().lower()
        source = source_value or "finlab"
        effective_asset_class = _resolve_asset_class(metadata.get("asset_class"), self.asset_class)

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
                market=self.market,
                asset_class=effective_asset_class,
                name=item.get("name") or base_name,
                open=self._parse_decimal(item.get("open")),
                high=self._parse_decimal(item.get("high")),
                low=self._parse_decimal(item.get("low")),
                close=self._parse_decimal(item.get("close")),
                volume=self._parse_int(volume_value),
                total_ticks=self._parse_int(item.get("total_ticks")),
                source=source,
                raw_data=item,
            )
            records.append(record)

        return records


class TWETFFinlabNormalizer(TWStockFinlabNormalizer):
    """Normalizer for TW ETF FinLab direct ingest payloads."""

    dataset_key = "tw_etf_eod"
    asset_class = "etf"


class WTXFinlabNormalizer(FuturesContinuousNormalizer):
    """Normalizer for WTX (TX 近月) FinLab direct ingest payloads.

    Expected payload shape:
        {
            "metadata": {"source": "finlab", "symbol": "WTX", "name": "台指近月", ...},
            "data": [
                {
                    "symbol": "TX一般",
                    "query_date": "2026-05-13",
                    "date": "2026-05-13",
                    "contract_month": "202605",
                    "open": ..., "high": ..., "low": ..., "close": ...,
                    "volume": ...,
                    "open_interest": ...
                }
            ]
        }
    """

    dataset_key = "wtx_eod"
    asset_class = "future"
    market = "WTX"

    def map_fields(self, raw_data: dict) -> list[FuturesContinuousRecord]:
        metadata = raw_data.get("metadata", {}) or {}
        data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        base_symbol = str(metadata.get("symbol") or "WTX").strip().upper()
        base_name = metadata.get("name")
        source_value = str(metadata.get("source") or "finlab").strip().lower()
        source = source_value or "finlab"

        records: list[FuturesContinuousRecord] = []
        for item in data_items:
            if not isinstance(item, dict):
                continue

            trade_date = self._parse_trade_date(item.get("date") or item.get("query_date"))
            if trade_date is None:
                continue

            symbol = str(item.get("symbol") or base_symbol or "").strip().upper()
            if not symbol:
                continue

            roll_rule_name = item.get("contract_month")
            record = FuturesContinuousRecord(
                symbol=symbol,
                trade_date=trade_date,
                name=item.get("name") or base_name,
                open=self._parse_decimal(item.get("open")),
                high=self._parse_decimal(item.get("high")),
                low=self._parse_decimal(item.get("low")),
                close=self._parse_decimal(item.get("close")),
                volume=self._parse_int(item.get("volume")),
                turnover=self._parse_decimal(item.get("turnover")),
                source=source,
                raw_data=item,
                identifier_type=None,
                identifier_value=None,
                roll_rule_name=str(roll_rule_name).strip() if roll_rule_name else None,
                roll_rule_description=None,
                roll_rule_config=None,
            )
            records.append(record)

        return records
