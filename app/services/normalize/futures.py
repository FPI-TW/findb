"""
Futures data normalizers.
"""

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.canonical import FuturesContinuousEOD, FuturesContract, RollRule
from app.services.dq.validators import DQIssueRecord
from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.normalize.types import FuturesContinuousRecord, FuturesContractRecord
from app.utils import utc_now, uuid7


def _merge_config(default: dict, override: dict | None) -> dict:
    """Merge dataset config with defaults."""
    if not override:
        return {**default}

    merged = {**default, **override}
    if "field_mapping" in override:
        merged["field_mapping"] = override["field_mapping"]
    return merged


class FuturesContractNormalizer(BaseNormalizer):
    """
    Normalizer for futures contract metadata.
    """

    dataset_key = "futures_contracts"
    asset_class = "future"
    market = "WTX"

    DEFAULT_CONFIG = {
        "data_path": "data",
        "symbol_field": "symbol",
        "name_field": "name",
        "source_field": "metadata.source",
        "identifier_field": "ticker",
        "identifier_type": "bloomberg",
        "field_mapping": {
            "contract_code": "contract_code",
            "contract_month": "contract_month",
            "expiry_date": "expiry_date",
            "currency": "currency",
            "extra": "extra",
        },
    }

    def __init__(self, db, dataset_config: dict | None = None):
        super().__init__(db, dataset_config)
        if self.dataset_config.get("asset_class"):
            self.asset_class = str(self.dataset_config["asset_class"])
        if self.dataset_config.get("market"):
            self.market = str(self.dataset_config["market"])

    def _parse_date(self, value: Any) -> Optional[date]:
        """Parse date value."""
        dt = self._parse_trade_date(value)
        return dt.date() if dt else None

    def _normalize_record(self, record: FuturesContractRecord) -> FuturesContractRecord:
        """Normalize futures contract record."""
        if record.symbol:
            record.symbol = str(record.symbol).upper().strip()
        elif record.identifier_value:
            record.symbol = str(record.identifier_value).split(" ")[0].upper()

        if record.contract_code:
            record.contract_code = str(record.contract_code).strip()
        if record.contract_month:
            record.contract_month = str(record.contract_month).strip()
        if record.currency:
            record.currency = str(record.currency).upper().strip()
        if record.source:
            record.source = str(record.source).lower().strip()
        return record

    def map_fields(self, raw_data: dict) -> list[FuturesContractRecord]:
        """Map futures contract payload to canonical format."""
        config = _merge_config(self.DEFAULT_CONFIG, getattr(self, "dataset_config", None))
        field_mapping = config.get("field_mapping", {}) or {}
        data_path = config.get("data_path", "data")
        symbol_path = field_mapping.get("symbol") or config.get("symbol_field", "symbol")
        name_path = field_mapping.get("name") or config.get("name_field", "name")
        source_path = field_mapping.get("source") or config.get("source_field")

        data_items = self._get_nested_value(raw_data, data_path) if data_path else None
        if data_items is None:
            data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        identifier_type = config.get("identifier_type")
        identifier_field = config.get("identifier_field")

        records: list[FuturesContractRecord] = []
        for item in data_items:
            contract_code = self._get_nested_value(item, field_mapping.get("contract_code"))
            contract_month = self._get_nested_value(item, field_mapping.get("contract_month"))
            expiry_value = self._get_nested_value(item, field_mapping.get("expiry_date"))
            currency_value = self._get_nested_value(item, field_mapping.get("currency"))
            extra_value = self._get_nested_value(item, field_mapping.get("extra"))

            identifier_value = self._get_nested_value(item, identifier_field)
            symbol_value = self._get_nested_value(item, symbol_path)
            name_value = self._get_nested_value(item, name_path)
            source_value = self._resolve_source(item, raw_data, source_path)

            record = FuturesContractRecord(
                symbol=symbol_value or "",
                contract_code=contract_code,
                contract_month=contract_month,
                expiry_date=self._parse_date(expiry_value),
                currency=currency_value,
                source=source_value,
                name=name_value,
                raw_data=item,
                extra=extra_value if isinstance(extra_value, dict) else None,
                identifier_type=identifier_type,
                identifier_value=identifier_value,
            )
            records.append(self._normalize_record(record))

        return records

    async def upsert_contract(
        self,
        instrument_id: UUID,
        record: FuturesContractRecord,
        run_id: UUID,
    ) -> None:
        """Upsert futures contract metadata."""
        stmt = insert(FuturesContract).values(
            contract_id=uuid7(),
            instrument_id=instrument_id,
            contract_code=record.contract_code or "",
            contract_month=record.contract_month,
            expiry_date=record.expiry_date,
            currency=record.currency,
            extra=record.extra,
            source=record.source,
            asof_ts=utc_now(),
            run_id=run_id,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_futures_contract",
            set_={
                "contract_month": stmt.excluded.contract_month,
                "expiry_date": stmt.excluded.expiry_date,
                "currency": stmt.excluded.currency,
                "extra": stmt.excluded.extra,
                "source": stmt.excluded.source,
                "asof_ts": stmt.excluded.asof_ts,
                "run_id": stmt.excluded.run_id,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await self.db.execute(stmt)

    async def process(self, raw_payload: dict, run_id: UUID) -> NormalizeResult:
        """Process futures contract payload and upsert into canonical storage."""
        result = NormalizeResult(run_id=run_id)

        try:
            await self.update_run_status(run_id, "processing", 0, 0, 0)

            mapped_records = self.map_fields(raw_payload)
            result.total_records = len(mapped_records)

            seen_keys: set[tuple[UUID, str]] = set()
            processed_records = 0

            for record in mapped_records:
                try:
                    issues: list[DQIssueRecord] = []

                    if not record.symbol and not record.identifier_value:
                        issues.append(
                            DQIssueRecord(
                                issue_type="MISSING_IDENTIFIER",
                                severity="error",
                                description="Missing symbol/identifier",
                                raw_data=record.raw_data,
                            )
                        )

                    if not record.contract_code:
                        if record.symbol:
                            record.contract_code = record.symbol
                        elif record.identifier_value:
                            record.contract_code = str(record.identifier_value).split(" ")[0]

                    if not record.contract_code:
                        issues.append(
                            DQIssueRecord(
                                issue_type="MISSING_CONTRACT_CODE",
                                severity="error",
                                description="Missing contract_code",
                                raw_data=record.raw_data,
                            )
                        )

                    if issues:
                        blocking = [i for i in issues if i.severity == "error"]
                        if blocking:
                            for issue in issues:
                                await self.record_dq_issue(issue, run_id)
                                result.dq_issues.append(issue)
                            result.failed_records += 1
                            continue

                    instrument = await self.resolve_instrument(record)
                    key = (instrument.instrument_id, record.contract_code or "")
                    if key in seen_keys:
                        issue = DQIssueRecord(
                            issue_type="DUPLICATE_KEY",
                            severity="error",
                            description="Duplicate contract in batch",
                            raw_data=record.raw_data,
                        )
                        await self.record_dq_issue(issue, run_id, instrument.instrument_id)
                        result.dq_issues.append(issue)
                        result.failed_records += 1
                        continue
                    seen_keys.add(key)

                    await self.upsert_contract(instrument.instrument_id, record, run_id)
                    result.success_records += 1
                finally:
                    processed_records += 1
                    await self._maybe_flush(processed_records)

            status = "completed" if result.failed_records == 0 else "completed_with_errors"
            await self.update_run_status(
                run_id,
                status,
                result.total_records,
                result.success_records,
                result.failed_records,
            )
            await self.db.commit()

        except Exception as e:
            result.error_message = str(e)
            await self.update_run_status(run_id, "failed", 0, 0, 0, str(e))
            await self.db.commit()

        return result


class FuturesContinuousNormalizer(BaseNormalizer):
    """
    Normalizer for continuous futures EOD data.
    """

    dataset_key = "futures_continuous_eod"
    asset_class = "future"
    market = "WTX"

    DEFAULT_CONFIG = {
        "data_path": "data",
        "symbol_field": "symbol",
        "name_field": "name",
        "source_field": "metadata.source",
        "identifier_field": "ticker",
        "identifier_type": "bloomberg",
        "field_mapping": {
            "trade_date": "trade_date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
            "turnover": "turnover",
            "roll_rule": "roll_rule",
            "roll_rule_name": "roll_rule.name",
            "roll_rule_description": "roll_rule.description",
            "roll_rule_config": "roll_rule.config",
        },
    }

    def __init__(self, db, dataset_config: dict | None = None):
        super().__init__(db, dataset_config)
        if self.dataset_config.get("asset_class"):
            self.asset_class = str(self.dataset_config["asset_class"])
        if self.dataset_config.get("market"):
            self.market = str(self.dataset_config["market"])
        self._roll_rule_cache: dict[str, RollRule] = {}

    def _normalize_record(self, record: FuturesContinuousRecord) -> FuturesContinuousRecord:
        """Normalize futures continuous record."""
        if record.symbol:
            record.symbol = str(record.symbol).upper().strip()
        elif record.identifier_value:
            record.symbol = str(record.identifier_value).split(" ")[0].upper()

        if record.identifier_value and not record.identifier_type:
            record.identifier_type = "bloomberg"

        if record.source:
            source_value = str(record.source).lower().strip()
            record.source = "bloomberg" if source_value.startswith("bloomberg") else source_value
        else:
            record.source = "bloomberg"

        return record

    def map_fields(self, raw_data: dict) -> list[FuturesContinuousRecord]:
        """Map futures continuous payload to canonical format."""
        config = _merge_config(self.DEFAULT_CONFIG, getattr(self, "dataset_config", None))
        field_mapping = config.get("field_mapping", {}) or {}
        data_path = config.get("data_path", "data")
        symbol_path = field_mapping.get("symbol") or config.get("symbol_field", "symbol")
        name_path = field_mapping.get("name") or config.get("name_field", "name")
        source_path = field_mapping.get("source") or config.get("source_field")

        data_items = self._get_nested_value(raw_data, data_path) if data_path else None
        if data_items is None:
            data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        identifier_type = config.get("identifier_type")
        identifier_field = config.get("identifier_field")

        records: list[FuturesContinuousRecord] = []
        for item in data_items:
            trade_date_value = self._get_nested_value(item, field_mapping.get("trade_date"))
            trade_date = self._parse_trade_date(trade_date_value)
            if trade_date is None:
                continue

            identifier_value = self._get_nested_value(item, identifier_field)
            symbol_value = self._get_nested_value(item, symbol_path)
            name_value = self._get_nested_value(item, name_path)
            source_value = self._resolve_source(item, raw_data, source_path)

            roll_rule_value = self._get_nested_value(item, field_mapping.get("roll_rule"))
            roll_rule_name = self._get_nested_value(item, field_mapping.get("roll_rule_name"))
            roll_rule_desc = self._get_nested_value(
                item, field_mapping.get("roll_rule_description")
            )
            roll_rule_config = self._get_nested_value(item, field_mapping.get("roll_rule_config"))
            if roll_rule_value and not roll_rule_name:
                roll_rule_name = roll_rule_value

            record = FuturesContinuousRecord(
                symbol=symbol_value or "",
                trade_date=trade_date,
                name=name_value,
                open=self._parse_decimal(self._get_nested_value(item, field_mapping.get("open"))),
                high=self._parse_decimal(self._get_nested_value(item, field_mapping.get("high"))),
                low=self._parse_decimal(self._get_nested_value(item, field_mapping.get("low"))),
                close=self._parse_decimal(self._get_nested_value(item, field_mapping.get("close"))),
                volume=self._parse_int(self._get_nested_value(item, field_mapping.get("volume"))),
                turnover=self._parse_decimal(
                    self._get_nested_value(item, field_mapping.get("turnover"))
                ),
                source=source_value,
                raw_data=item,
                identifier_type=identifier_type,
                identifier_value=identifier_value,
                roll_rule_name=str(roll_rule_name).strip() if roll_rule_name else None,
                roll_rule_description=str(roll_rule_desc).strip() if roll_rule_desc else None,
                roll_rule_config=roll_rule_config if isinstance(roll_rule_config, dict) else None,
            )
            records.append(self._normalize_record(record))

        return records

    async def get_or_create_roll_rule(
        self,
        name: str | None,
        description: str | None,
        config: dict | None,
    ) -> UUID | None:
        """Get or create roll rule by name."""
        if not name:
            return None

        cached = self._roll_rule_cache.get(name)
        if cached is not None:
            updated = False
            if description and cached.description != description:
                cached.description = description
                updated = True
            if config and cached.config != config:
                cached.config = config
                updated = True
            if updated:
                cached.updated_at = utc_now()
            return cached.rule_id

        stmt = select(RollRule).where(RollRule.name == name)
        result = await self.db.execute(stmt)
        rule = result.scalar_one_or_none()

        if rule:
            updated = False
            if description and rule.description != description:
                rule.description = description
                updated = True
            if config and rule.config != config:
                rule.config = config
                updated = True
            if updated:
                rule.updated_at = utc_now()
            self._roll_rule_cache[name] = rule
            return rule.rule_id

        rule = RollRule(
            rule_id=uuid7(),
            name=name,
            description=description,
            config=config,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.db.add(rule)
        await self.db.flush()
        self._roll_rule_cache[name] = rule
        return rule.rule_id

    async def check_duplicate_in_db(
        self,
        instrument_id: UUID,
        trade_date: datetime | date,
    ) -> bool:
        """Check if instrument/trade_date already exists in the database."""
        trade_date_value = trade_date.date() if isinstance(trade_date, datetime) else trade_date
        stmt = select(FuturesContinuousEOD.id).where(
            FuturesContinuousEOD.instrument_id == instrument_id,
            FuturesContinuousEOD.trade_date == trade_date_value,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def upsert_continuous_eod(
        self,
        instrument_id: UUID,
        record: FuturesContinuousRecord,
        run_id: UUID,
        roll_rule_id: UUID | None,
    ) -> None:
        """Upsert continuous futures EOD record."""
        stmt = insert(FuturesContinuousEOD).values(
            id=uuid7(),
            instrument_id=instrument_id,
            roll_rule_id=roll_rule_id,
            trade_date=(
                record.trade_date.date()
                if isinstance(record.trade_date, datetime)
                else record.trade_date
            ),
            open=record.open,
            high=record.high,
            low=record.low,
            close=record.close,
            volume=record.volume,
            turnover=record.turnover,
            source=record.source,
            asof_ts=utc_now(),
            run_id=run_id,
            created_at=utc_now(),
            updated_at=utc_now(),
        )

        stmt = stmt.on_conflict_do_update(
            constraint="uq_futures_cont_eod",
            set_={
                "roll_rule_id": stmt.excluded.roll_rule_id,
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "turnover": stmt.excluded.turnover,
                "source": stmt.excluded.source,
                "asof_ts": stmt.excluded.asof_ts,
                "run_id": stmt.excluded.run_id,
                "updated_at": stmt.excluded.updated_at,
            },
        )

        await self.db.execute(stmt)
        self.db.expire_all()
        self._instrument_cache.clear()
        self._identifier_cache.clear()
        self._roll_rule_cache.clear()

    async def process(self, raw_payload: dict, run_id: UUID) -> NormalizeResult:
        """Process futures continuous payload and upsert into canonical storage."""
        result = NormalizeResult(run_id=run_id)

        try:
            await self.update_run_status(run_id, "processing", 0, 0, 0)

            mapped_records = self.map_fields(raw_payload)
            result.total_records = len(mapped_records)

            seen_keys: set[tuple[str, str, datetime]] = set()
            processed_records = 0

            for record in mapped_records:
                try:
                    issues = self.dq_validator.validate_eod(record, seen_keys=seen_keys)
                    blocking_issues = [i for i in issues if i.severity == "error"]

                    if blocking_issues:
                        for issue in issues:
                            await self.record_dq_issue(issue, run_id)
                            result.dq_issues.append(issue)
                        result.failed_records += 1
                        continue

                    instrument = await self.resolve_instrument(record)

                    delisted_issue = self._check_delisted_instrument(
                        instrument,
                        record.trade_date,
                        record.raw_data,
                    )
                    if delisted_issue:
                        issues.append(delisted_issue)
                        for issue in issues:
                            await self.record_dq_issue(
                                issue,
                                run_id,
                                instrument.instrument_id,
                            )
                            result.dq_issues.append(issue)
                        result.failed_records += 1
                        continue
                    await self.get_or_create_trading_day(record.trade_date)

                    blocking_issues = [i for i in issues if i.severity == "error"]
                    if blocking_issues:
                        for issue in issues:
                            await self.record_dq_issue(
                                issue,
                                run_id,
                                instrument.instrument_id,
                            )
                            result.dq_issues.append(issue)
                        result.failed_records += 1
                        continue

                    roll_rule_id = await self.get_or_create_roll_rule(
                        record.roll_rule_name,
                        record.roll_rule_description,
                        record.roll_rule_config,
                    )

                    for issue in issues:
                        await self.record_dq_issue(
                            issue,
                            run_id,
                            instrument.instrument_id,
                        )
                        result.dq_issues.append(issue)

                    await self.upsert_continuous_eod(
                        instrument.instrument_id,
                        record,
                        run_id,
                        roll_rule_id,
                    )
                    result.success_records += 1

                except Exception as e:
                    result.failed_records += 1
                    result.dq_issues.append(
                        DQIssueRecord(
                            issue_type="processing_error",
                            severity="error",
                            description=str(e),
                            raw_data=record.raw_data,
                        )
                    )
                finally:
                    processed_records += 1
                    await self._maybe_flush(processed_records)

            status = "completed" if result.failed_records == 0 else "completed_with_errors"
            await self.update_run_status(
                run_id,
                status,
                result.total_records,
                result.success_records,
                result.failed_records,
            )
            await self.db.commit()

        except Exception as e:
            result.error_message = str(e)
            await self.update_run_status(run_id, "failed", 0, 0, 0, str(e))
            await self.db.commit()

        return result


class WTXBloombergNormalizer(FuturesContinuousNormalizer):
    """
    Normalizer for WTX futures continuous EOD data from Bloomberg API direct format.
    Writes to futures_continuous_eod; roll_rule fields are None (not provided by Bloomberg API).

    Expected data format:
    {
        "metadata": {"source": "Bloomberg API", "category": "Futures"},
        "data": [
            {
                "symbol": "TXF1",
                "name": "TAIFEX TX Futures 1",
                "ticker": "TXF1 Index",
                "price": {"last": 21000.0, "open": 20800.0, "high": 21100.0, "low": 20700.0,
                          "volume": 50000},
                "timestamp": {"query_time": "...", "last_update": "2026-03-12"},
                "metadata": {"source": "Bloomberg"}
            }
        ]
    }
    """

    dataset_key = "wtx_eod"
    asset_class = "future"
    market = "WTX"

    def map_fields(self, raw_data: dict) -> list[FuturesContinuousRecord]:
        """Map Bloomberg futures data to canonical format."""
        data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        records: list[FuturesContinuousRecord] = []

        for item in data_items:
            timestamp = item.get("timestamp", {})
            trade_date_str = (
                timestamp.get("last_update") or item.get("date") or timestamp.get("query_time")
            )
            trade_date = self._parse_trade_date(trade_date_str)
            if trade_date is None:
                continue

            symbol = item.get("symbol")
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
            turnover = self._parse_decimal(price.get("turnover") or item.get("turnover"))

            item_metadata = item.get("metadata", {})
            source = item_metadata.get("source") or raw_data.get("metadata", {}).get("source")
            if source:
                source_lower = source.lower()
                source = "bloomberg" if source_lower.startswith("bloomberg") else source_lower
            else:
                source = "bloomberg"

            record = FuturesContinuousRecord(
                symbol=symbol.upper() if symbol else "",
                trade_date=trade_date,
                name=name,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                turnover=turnover,
                source=source,
                raw_data=item,
                identifier_type="bloomberg" if ticker else None,
                identifier_value=ticker,
                roll_rule_name=None,
                roll_rule_description=None,
                roll_rule_config=None,
            )
            records.append(self._normalize_record(record))

        return records
