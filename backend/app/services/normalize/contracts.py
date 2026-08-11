"""Normalizers for provider-neutral, versioned ingress contracts."""

import hashlib
from datetime import date, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.models.canonical import Instrument, MarketDataMinute
from app.services.dq.validators import DQIssueRecord
from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.normalize.types import MappedRecord, MarketMinuteRecord
from app.utils import utc_now
from app.utils.datetime_utils import ensure_utc, parse_datetime


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


class MarketMinuteContractNormalizer(BaseNormalizer):
    """Normalize and persist provider-neutral ``market_minute.v1`` bars."""

    dataset_key = "market_minute"
    asset_class = "equity"
    market = "TW"

    def __init__(self, db, dataset_config: dict | None = None):
        super().__init__(db, dataset_config)
        market, asset_class, _, _ = _contract_context(self.dataset_config)
        self.market = market
        self.asset_class = asset_class

    @staticmethod
    def _parse_utc_timestamp(value: object) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = value if isinstance(value, datetime) else parse_datetime(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return ensure_utc(parsed)

    @staticmethod
    def _parse_local_trade_date(value: object) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None

    def map_fields(self, raw_data: dict) -> list[MarketMinuteRecord]:
        data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []
        market, asset_class, default_currency, source = _contract_context(self.dataset_config)
        records: list[MarketMinuteRecord] = []
        for item in data_items:
            if not isinstance(item, dict):
                continue
            trade_date = self._parse_local_trade_date(item.get("trade_date"))
            symbol = str(item.get("symbol") or "").strip().upper()
            if trade_date is None or not symbol:
                continue
            source_symbol = item.get("source_symbol")
            identifier_value = str(source_symbol).strip() if source_symbol else None
            currency_value = item.get("currency") or default_currency
            currency = str(currency_value).strip().upper() if currency_value else None
            records.append(
                MarketMinuteRecord(
                    symbol=symbol,
                    trade_date=trade_date,
                    bar_start_time=self._parse_utc_timestamp(item.get("bar_start_time")),
                    bar_end_time=self._parse_utc_timestamp(item.get("bar_end_time")),
                    signal_time=self._parse_utc_timestamp(item.get("signal_time")),
                    market_timezone=(
                        str(item.get("market_timezone")).strip()
                        if item.get("market_timezone")
                        else None
                    ),
                    price_adjustment=(
                        str(item.get("price_adjustment")).strip()
                        if item.get("price_adjustment")
                        else None
                    ),
                    open=self._parse_decimal(item.get("open")),
                    high=self._parse_decimal(item.get("high")),
                    low=self._parse_decimal(item.get("low")),
                    close=self._parse_decimal(item.get("close")),
                    volume=self._parse_int(item.get("volume")),
                    turnover=self._parse_decimal(item.get("turnover")),
                    trade_count=self._parse_int(item.get("trade_count")),
                    market=market,
                    asset_class=asset_class,
                    name=item.get("name"),
                    currency=currency,
                    source=source,
                    raw_data=item,
                    identifier_type=_identifier_type(source) if identifier_value else None,
                    identifier_value=identifier_value,
                )
            )
        return records

    def _anomalies_for_record(self, raw_payload: dict, record: MarketMinuteRecord) -> list[dict]:
        batch = raw_payload.get("batch")
        anomalies = batch.get("anomalies", []) if isinstance(batch, dict) else []
        if record.bar_start_time is None or not isinstance(anomalies, list):
            return []
        matched: list[dict] = []
        for anomaly in anomalies:
            if (
                not isinstance(anomaly, dict)
                or str(anomaly.get("symbol") or "").strip().upper() != record.symbol
            ):
                continue
            if self._parse_utc_timestamp(anomaly.get("bar_start_time")) == record.bar_start_time:
                matched.append(anomaly)
        return matched

    async def upsert_minute(self, instrument_id, record: MarketMinuteRecord, run_id) -> bool:
        assert record.bar_start_time is not None
        assert record.bar_end_time is not None
        assert record.signal_time is not None
        source_priority, source_fetched_at = self._source_control_values(record.source)
        stmt = insert(MarketDataMinute).values(
            instrument_id=instrument_id,
            trade_date=record.trade_date,
            bar_start_time=record.bar_start_time,
            bar_end_time=record.bar_end_time,
            signal_time=record.signal_time,
            market_timezone=record.market_timezone,
            open=record.open,
            high=record.high,
            low=record.low,
            close=record.close,
            volume=record.volume,
            turnover=record.turnover,
            trade_count=record.trade_count,
            price_adjustment=record.price_adjustment,
            source=record.source,
            source_priority=source_priority,
            source_fetched_at=source_fetched_at,
            asof_ts=utc_now(),
            run_id=run_id,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        table = MarketDataMinute.__table__
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "trade_date", "bar_start_time"],
            set_={
                column: getattr(stmt.excluded, column)
                for column in (
                    "bar_end_time",
                    "signal_time",
                    "market_timezone",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "turnover",
                    "trade_count",
                    "price_adjustment",
                    "source",
                    "source_priority",
                    "source_fetched_at",
                    "asof_ts",
                    "run_id",
                    "updated_at",
                )
            },
            where=self._incoming_source_wins(table, stmt.excluded),
        )
        applied = (
            await self.db.execute(stmt.returning(MarketDataMinute.instrument_id))
        ).scalar_one_or_none() is not None
        self.db.expire_all()
        self._instrument_cache.clear()
        self._identifier_cache.clear()
        return applied

    async def process(self, raw_payload: dict, run_id, *, commit: bool = True) -> NormalizeResult:
        """Process minute bars without EOD stats or partition lifecycle side effects."""
        result = NormalizeResult(run_id=run_id)
        try:
            await self.update_run_status(run_id, "processing", 0, 0, 0)
            records = self.map_fields(raw_payload)
            result.total_records = len(records)
            key_counts: dict[tuple[str, str, datetime], int] = {}
            for record in records:
                if record.bar_start_time is None:
                    continue
                key = (str(record.market or "").upper(), record.symbol, record.bar_start_time)
                key_counts[key] = key_counts.get(key, 0) + 1
            seen_keys = {key for key, count in key_counts.items() if count > 1}
            for processed, record in enumerate(records, start=1):
                instrument: Instrument | None = None
                try:
                    anomalies = self._anomalies_for_record(raw_payload, record)
                    anomaly_fields = {str(anomaly.get("field")) for anomaly in anomalies}
                    issues = self.dq_validator.validate_minute(
                        record, seen_keys=seen_keys, anomaly_fields=anomaly_fields
                    )
                    if any(issue.severity == "error" for issue in issues):
                        for issue in issues:
                            await self.record_dq_issue(issue, run_id)
                            result.dq_issues.append(issue)
                        result.failed_records += 1
                        continue
                    instrument = await self.resolve_instrument(record)
                    delisted_issue = self._check_delisted_instrument(
                        instrument, record.trade_date, record.raw_data
                    )
                    if delisted_issue:
                        issues.append(delisted_issue)
                    for anomaly in anomalies:
                        issues.append(
                            DQIssueRecord(
                                issue_type="MINUTE_ADAPTER_ANOMALY",
                                severity="warning",
                                description=(
                                    f"Adapter reported {anomaly.get('field')}: {anomaly.get('reason')}"
                                ),
                                trade_date=record.bar_start_time,
                                raw_data=anomaly,
                            )
                        )
                    if any(issue.severity == "error" for issue in issues):
                        for issue in issues:
                            await self.record_dq_issue(issue, run_id, instrument.instrument_id)
                            result.dq_issues.append(issue)
                        result.failed_records += 1
                        continue
                    await self.get_or_create_trading_day(record.trade_date, market=record.market)
                    for issue in issues:
                        await self.record_dq_issue(issue, run_id, instrument.instrument_id)
                        result.dq_issues.append(issue)
                    if await self.upsert_minute(instrument.instrument_id, record, run_id):
                        result.success_records += 1
                    else:
                        result.precedence_rejected_records += 1
                except Exception as exc:
                    if isinstance(exc, SQLAlchemyError):
                        raise
                    result.failed_records += 1
                    issue = DQIssueRecord(
                        issue_type="processing_error",
                        severity="error",
                        description=type(exc).__name__,
                        trade_date=record.bar_start_time,
                        raw_data=record.raw_data,
                    )
                    await self.record_dq_issue(
                        issue, run_id, instrument.instrument_id if instrument else None
                    )
                    result.dq_issues.append(issue)
                finally:
                    await self._maybe_flush(processed)
            await self.record_precedence_rejection_summary(result, run_id)
            await self.update_run_status(
                run_id,
                "completed" if result.failed_records == 0 else "completed_with_errors",
                result.total_records,
                result.success_records,
                result.failed_records,
                self._summarize_processing_errors(result.dq_issues),
            )
            if commit:
                await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return result
