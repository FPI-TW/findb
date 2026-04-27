"""
Corporate action data normalizer.
"""

from datetime import date, datetime, time
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert

from app.models.canonical import CorporateAction
from app.services.dq.validators import DQIssueRecord
from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.normalize.types import CorporateActionRecord
from app.utils import utc_now, uuid7
from app.utils.datetime_utils import ensure_utc


def _merge_config(default: dict, override: dict | None) -> dict:
    """Merge dataset config with defaults."""
    if not override:
        return {**default}

    merged = {**default, **override}
    if "field_mapping" in override:
        merged["field_mapping"] = override["field_mapping"]
    return merged


class CorporateActionNormalizer(BaseNormalizer):
    """
    Normalizer for corporate actions (dividends, splits, etc.).
    """

    dataset_key = "us_equity_corporate_actions"
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
            "action_type": "action.type",
            "ex_date": "action.ex_date",
            "record_date": "action.record_date",
            "pay_date": "action.pay_date",
            "ratio": "action.ratio",
            "cash_amount": "action.cash_amount",
            "currency": "action.currency",
        },
    }

    def _parse_action_date(self, value: Any) -> Optional[date]:
        """Parse corporate action date to date object."""
        dt = self._parse_trade_date(value)
        return dt.date() if dt else None

    def _to_datetime(self, value: date | datetime | None) -> Optional[datetime]:
        """Normalize date/datetime to UTC-aware datetime."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return ensure_utc(value)
        return ensure_utc(datetime.combine(value, time.min))

    def _get_action_value(
        self,
        item: dict,
        path: str | None,
        fallback_keys: list[str],
    ) -> Any:
        """Get value from config path with fallback keys."""
        if path:
            value = self._get_nested_value(item, path)
            if value is not None:
                return value
        for key in fallback_keys:
            value = self._get_nested_value(item, key)
            if value is not None:
                return value
        return None

    def _normalize_record(self, record: CorporateActionRecord) -> CorporateActionRecord:
        """Normalize symbol, source, and identifier fields."""
        if record.symbol:
            record.symbol = record.symbol.upper()
        elif record.identifier_value:
            record.symbol = str(record.identifier_value).split(" ")[0].upper()

        if record.identifier_value and not record.identifier_type:
            record.identifier_type = "bloomberg"

        if record.source:
            normalized = record.source.lower()
            record.source = "bloomberg" if normalized.startswith("bloomberg") else normalized
        else:
            record.source = "bloomberg"

        if isinstance(record.action_type, str):
            record.action_type = record.action_type.lower()

        if record.currency:
            record.currency = record.currency.upper()

        return record

    def map_fields(self, raw_data: dict) -> list[CorporateActionRecord]:
        """Map corporate action payload to canonical format."""
        config = _merge_config(self.DEFAULT_CONFIG, getattr(self, "dataset_config", None))
        field_mapping = config.get("field_mapping", {})
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

        records: list[CorporateActionRecord] = []
        for item in data_items:
            action_type_value = self._get_action_value(
                item,
                field_mapping.get("action_type"),
                ["action_type", "action.type", "type"],
            )
            ex_date_value = self._get_action_value(
                item,
                field_mapping.get("ex_date"),
                ["ex_date", "exDate", "action.ex_date"],
            )
            record_date_value = self._get_action_value(
                item,
                field_mapping.get("record_date"),
                ["record_date", "recordDate", "action.record_date"],
            )
            pay_date_value = self._get_action_value(
                item,
                field_mapping.get("pay_date"),
                ["pay_date", "payDate", "action.pay_date"],
            )
            ratio_value = self._get_action_value(
                item,
                field_mapping.get("ratio"),
                ["ratio", "split_ratio", "action.ratio"],
            )
            cash_amount_value = self._get_action_value(
                item,
                field_mapping.get("cash_amount"),
                ["cash_amount", "cashAmount", "amount", "action.cash_amount"],
            )
            currency_value = self._get_action_value(
                item,
                field_mapping.get("currency"),
                ["currency", "action.currency"],
            )

            identifier_value = self._get_nested_value(item, identifier_field)
            symbol_value = self._get_nested_value(item, symbol_path)
            name_value = self._get_nested_value(item, name_path)
            source_value = self._resolve_source(item, raw_data, source_path)

            record = CorporateActionRecord(
                symbol=symbol_value or "",
                action_type=(
                    str(action_type_value).strip() if action_type_value is not None else None
                ),
                ex_date=self._parse_action_date(ex_date_value),
                record_date=self._parse_action_date(record_date_value),
                pay_date=self._parse_action_date(pay_date_value),
                ratio=self._parse_decimal(ratio_value),
                cash_amount=self._parse_decimal(cash_amount_value),
                currency=str(currency_value).strip() if currency_value is not None else None,
                source=source_value,
                name=name_value,
                raw_data=item,
                extra=item.get("extra") if isinstance(item, dict) else None,
                identifier_type=identifier_type,
                identifier_value=identifier_value,
            )
            records.append(self._normalize_record(record))

        return records

    async def process(self, raw_payload: dict, run_id: UUID) -> NormalizeResult:
        """
        Process corporate action payload and upsert into canonical storage.
        Optimized with PostgreSQL ON CONFLICT to prevent race conditions.
        """
        result = NormalizeResult(run_id=run_id)

        try:
            await self.update_run_status(run_id, "processing", 0, 0, 0)

            mapped_records = self.map_fields(raw_payload)
            result.total_records = len(mapped_records)

            seen_keys: set[tuple] = set()
            processed_records = 0

            for record in mapped_records:
                try:
                    issues: list[DQIssueRecord] = []

                    if not record.action_type:
                        trade_date_val = None
                        if record.ex_date:
                            try:
                                trade_date_val = self._to_datetime(record.ex_date)
                            except Exception:
                                trade_date_val = None

                        issues.append(
                            DQIssueRecord(
                                issue_type="MISSING_ACTION_TYPE",
                                severity="error",
                                description="Missing action_type",
                                trade_date=trade_date_val,
                                raw_data=record.raw_data,
                            )
                        )

                    if not record.ex_date:
                        issues.append(
                            DQIssueRecord(
                                issue_type="MISSING_EX_DATE",
                                severity="error",
                                description="Missing ex_date",
                                raw_data=record.raw_data,
                            )
                        )

                    if not record.symbol and not record.identifier_value:
                        issues.append(
                            DQIssueRecord(
                                issue_type="MISSING_IDENTIFIER",
                                severity="error",
                                description="Missing symbol/identifier",
                                raw_data=record.raw_data,
                            )
                        )

                    if issues:
                        for issue in issues:
                            await self.record_dq_issue(issue, run_id)
                            result.dq_issues.append(issue)
                        result.failed_records += 1
                        continue

                    instrument = await self.resolve_instrument(record)

                    key = (instrument.instrument_id, record.action_type, record.ex_date)
                    if key in seen_keys:
                        issue = DQIssueRecord(
                            issue_type="DUPLICATE_KEY",
                            severity="warning",
                            description="Duplicate corporate action in same batch",
                            trade_date=self._to_datetime(record.ex_date),
                            raw_data=record.raw_data,
                        )
                        await self.record_dq_issue(issue, run_id, instrument.instrument_id)
                        result.dq_issues.append(issue)
                        result.failed_records += 1
                        continue
                    seen_keys.add(key)

                    now = utc_now()
                    insert_stmt = insert(CorporateAction).values(
                        action_id=uuid7(),
                        instrument_id=instrument.instrument_id,
                        action_type=record.action_type or "",
                        ex_date=record.ex_date,
                        record_date=record.record_date,
                        pay_date=record.pay_date,
                        ratio=record.ratio,
                        cash_amount=record.cash_amount,
                        currency=record.currency,
                        extra=record.extra,
                        source=record.source,
                        asof_ts=now,
                        run_id=run_id,
                        created_at=now,
                        updated_at=now,
                    )

                    upsert_stmt = insert_stmt.on_conflict_do_update(
                        constraint="uq_ca_instrument_action_date",
                        set_={
                            "record_date": insert_stmt.excluded.record_date,
                            "pay_date": insert_stmt.excluded.pay_date,
                            "ratio": insert_stmt.excluded.ratio,
                            "cash_amount": insert_stmt.excluded.cash_amount,
                            "currency": insert_stmt.excluded.currency,
                            "extra": insert_stmt.excluded.extra,
                            "source": insert_stmt.excluded.source,
                            "asof_ts": insert_stmt.excluded.asof_ts,
                            "run_id": insert_stmt.excluded.run_id,
                            "updated_at": now,
                        },
                    )

                    await self.db.execute(upsert_stmt)
                    result.success_records += 1

                except Exception as e:
                    result.failed_records += 1
                    await self.record_dq_issue(
                        DQIssueRecord(
                            issue_type="PROCESSING_ERROR",
                            severity="error",
                            description=str(e),
                            raw_data=record.raw_data,
                        ),
                        run_id,
                    )
                finally:
                    processed_records += 1
                    await self._maybe_flush(processed_records)

            status = "completed" if result.failed_records == 0 else "completed_with_errors"
            await self.update_run_status(
                run_id, status, result.total_records, result.success_records, result.failed_records
            )
            await self.db.commit()

        except Exception as e:
            await self.db.rollback()
            result.error_message = str(e)
            await self.update_run_status(run_id, "failed", 0, 0, 0, str(e))
            await self.db.commit()

        return result
