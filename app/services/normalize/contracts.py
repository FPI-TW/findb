"""Normalizers for provider-neutral, versioned ingress contracts."""

import hashlib

from app.services.normalize.base import BaseNormalizer
from app.services.normalize.futures import FuturesContinuousNormalizer
from app.services.normalize.types import FuturesContinuousRecord, MappedRecord


def _contract_context(config: dict) -> tuple[str, str, str | None, str | None]:
    defaults = config["defaults"]
    market = str(defaults["market"]).strip().upper()
    asset_class = str(defaults["asset_class"]).strip().lower()
    currency_value = defaults.get("currency")
    currency = str(currency_value).strip().upper() if currency_value else None
    source_value = config.get("_ingest_source")
    source = str(source_value).strip().lower() if source_value else None
    return market, asset_class, currency, source


def _identifier_type(source: str | None) -> str | None:
    if source is None:
        return None
    if len(source) <= 30:
        return source
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:26]
    return f"src_{digest}"


class MarketEODContractNormalizer(BaseNormalizer):
    """Normalize the provider-neutral ``market_eod.v1`` row shape."""

    dataset_key = "market_eod"
    asset_class = "equity"
    market = "GLOBAL"

    def __init__(self, db, dataset_config: dict | None = None):
        super().__init__(db, dataset_config)
        market, asset_class, _, _ = _contract_context(self.dataset_config)
        self.market = market
        self.asset_class = asset_class

    def map_fields(self, raw_data: dict) -> list[MappedRecord]:
        data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []
        market, asset_class, default_currency, source = _contract_context(self.dataset_config)

        records: list[MappedRecord] = []
        for item in data_items:
            if not isinstance(item, dict):
                continue
            trade_date = self._parse_trade_date(item.get("trade_date"))
            symbol = str(item.get("symbol") or "").strip().upper()
            if trade_date is None or not symbol:
                continue

            source_symbol = item.get("source_symbol")
            identifier_value = str(source_symbol).strip() if source_symbol else None
            currency_value = item.get("currency") or default_currency
            currency = str(currency_value).strip().upper() if currency_value else None
            records.append(
                MappedRecord(
                    symbol=symbol,
                    trade_date=trade_date,
                    market=market,
                    asset_class=asset_class,
                    name=item.get("name"),
                    currency=currency,
                    open=self._parse_decimal(item.get("open")),
                    high=self._parse_decimal(item.get("high")),
                    low=self._parse_decimal(item.get("low")),
                    close=self._parse_decimal(item.get("close")),
                    volume=self._parse_int(item.get("volume")),
                    turnover=self._parse_decimal(item.get("turnover")),
                    total_ticks=self._parse_int(item.get("total_ticks")),
                    source=source,
                    raw_data=item,
                    identifier_type=_identifier_type(source) if identifier_value else None,
                    identifier_value=identifier_value,
                )
            )
        return records


class FuturesContinuousEODContractNormalizer(FuturesContinuousNormalizer):
    """Normalize the provider-neutral ``futures_continuous_eod.v1`` row shape."""

    dataset_key = "futures_continuous_eod"
    asset_class = "future"
    market = "WTX"

    def __init__(self, db, dataset_config: dict | None = None):
        super().__init__(db, dataset_config)
        market, asset_class, _, _ = _contract_context(self.dataset_config)
        self.market = market
        self.asset_class = asset_class

    def map_fields(self, raw_data: dict) -> list[FuturesContinuousRecord]:
        data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []
        _, _, default_currency, source = _contract_context(self.dataset_config)

        records: list[FuturesContinuousRecord] = []
        for item in data_items:
            if not isinstance(item, dict):
                continue
            trade_date = self._parse_trade_date(item.get("trade_date"))
            symbol = str(item.get("symbol") or "").strip().upper()
            if trade_date is None or not symbol:
                continue

            source_symbol = item.get("source_symbol")
            identifier_value = str(source_symbol).strip() if source_symbol else None
            records.append(
                FuturesContinuousRecord(
                    symbol=symbol,
                    trade_date=trade_date,
                    name=item.get("name"),
                    currency=default_currency,
                    open=self._parse_decimal(item.get("open")),
                    high=self._parse_decimal(item.get("high")),
                    low=self._parse_decimal(item.get("low")),
                    close=self._parse_decimal(item.get("close")),
                    volume=self._parse_int(item.get("volume")),
                    turnover=self._parse_decimal(item.get("turnover")),
                    source=source,
                    raw_data=item,
                    identifier_type=_identifier_type(source) if identifier_value else None,
                    identifier_value=identifier_value,
                    roll_rule_name=str(item["roll_rule"]).strip(),
                    roll_rule_description=None,
                    roll_rule_config=None,
                )
            )
        return records
