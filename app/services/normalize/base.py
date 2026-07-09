"""
Base Normalizer class for data normalization.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import (
    Instrument,
    InstrumentIdentifier,
    MarketDataEOD,
    TradingCalendar,
)
from app.models.registry import DQIssue, IngestionRun
from app.services.dq.validators import DQIssueRecord, DQValidator
from app.services.normalize.types import InstrumentResolvableRecord, MappedRecord
from app.utils import utc_now, uuid7
from app.utils.datetime_utils import ensure_utc, parse_datetime
from app.vocabulary import normalize_asset_class, normalize_market


@dataclass
class NormalizeResult:
    """Result of normalization process."""

    run_id: UUID
    total_records: int = 0
    success_records: int = 0
    failed_records: int = 0
    dq_issues: list[DQIssueRecord] = field(default_factory=list)
    error_message: Optional[str] = None


class BaseNormalizer(ABC):
    """
    Abstract base class for data normalizers.
    Each market/dataset type should implement its own normalizer.
    """

    dataset_key: str = ""
    asset_class: str = ""
    market: str = ""

    def __init__(self, db: AsyncSession, dataset_config: dict | None = None):
        self.db = db
        self.dataset_config = dataset_config or {}
        self.dq_validator = DQValidator()
        self._flush_interval = 1000
        self._instrument_cache: dict[tuple[str, str, str], Instrument] = {}
        self._identifier_cache: dict[tuple[str, str, str, str], Instrument | None] = {}
        self._identifier_exists_cache: set[tuple[str, str]] = set()
        self._trading_day_cache: set[tuple[str, date]] = set()
        self._eod_partition_cache: set[int] = set()

    def _get_nested_value(self, data: dict, path: str | None) -> Any:
        """Get value from nested dict using dot notation."""
        if not path:
            return None
        value: Any = data
        for key in path.split("."):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value

    def _parse_decimal(self, value: Any) -> Optional[Decimal]:
        """Parse value into Decimal."""
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (TypeError, ValueError):
            return None

    def _parse_int(self, value: Any) -> Optional[int]:
        """Parse value into int."""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _parse_trade_date(self, value: Any) -> Optional[datetime]:
        """Parse trade date into UTC-aware datetime."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return ensure_utc(value)
        if isinstance(value, date):
            return ensure_utc(datetime.combine(value, time.min))
        try:
            return ensure_utc(parse_datetime(str(value)))
        except ValueError:
            return None

    def _resolve_source(self, item: dict, raw_data: dict, source_path: str | None) -> Optional[str]:
        """Resolve source value from config or metadata."""
        source = None
        if source_path:
            source = self._get_nested_value(item, source_path)
            if source is None:
                source = self._get_nested_value(raw_data, source_path)
        if source is None:
            source = self._get_nested_value(item, "metadata.source")
        if source is None:
            source = self._get_nested_value(raw_data, "metadata.source")
        return source

    def map_fields_from_config(
        self,
        raw_data: dict,
        config_override: dict | None = None,
    ) -> list[MappedRecord]:
        """Map raw data fields using dataset config mapping."""
        config = config_override or getattr(self, "dataset_config", {}) or {}
        field_mapping = config.get("field_mapping", {})
        data_path = config.get("data_path", "data")
        symbol_path = field_mapping.get("symbol") or config.get("symbol_field", "symbol")
        market_path = field_mapping.get("market") or config.get("market_field")
        name_path = field_mapping.get("name") or config.get("name_field", "name")
        source_path = field_mapping.get("source") or config.get("source_field")

        data_items = self._get_nested_value(raw_data, data_path) if data_path else None
        if data_items is None:
            data_items = raw_data.get("data", [])
        if not isinstance(data_items, list):
            return []

        identifier_type = config.get("identifier_type")
        identifier_field = config.get("identifier_field")

        records: list[MappedRecord] = []
        for item in data_items:
            trade_date_value = self._get_nested_value(item, field_mapping.get("trade_date"))
            trade_date = self._parse_trade_date(trade_date_value)
            if trade_date is None:
                continue

            identifier_value = self._get_nested_value(item, identifier_field)
            symbol_value = self._get_nested_value(item, symbol_path)
            market_value = self._get_nested_value(item, market_path) if market_path else None
            name_value = self._get_nested_value(item, name_path)
            source_value = self._resolve_source(item, raw_data, source_path)

            if not symbol_value and not identifier_value:
                continue

            record = MappedRecord(
                symbol=symbol_value or "",
                trade_date=trade_date,
                market=str(market_value).upper().strip() if market_value is not None else None,
                name=name_value,
                open=self._parse_decimal(self._get_nested_value(item, field_mapping.get("open"))),
                high=self._parse_decimal(self._get_nested_value(item, field_mapping.get("high"))),
                low=self._parse_decimal(self._get_nested_value(item, field_mapping.get("low"))),
                close=self._parse_decimal(self._get_nested_value(item, field_mapping.get("close"))),
                volume=self._parse_int(self._get_nested_value(item, field_mapping.get("volume"))),
                total_ticks=self._parse_int(
                    self._get_nested_value(item, field_mapping.get("total_ticks"))
                ),
                turnover=self._parse_decimal(
                    self._get_nested_value(item, field_mapping.get("turnover"))
                ),
                source=source_value,
                raw_data=item,
                identifier_type=identifier_type,
                identifier_value=identifier_value,
            )
            records.append(record)

        return records

    @abstractmethod
    def map_fields(self, raw_data: dict) -> list[Any]:
        """
        Map raw data fields to canonical format.
        Must be implemented by subclasses.

        Args:
            raw_data: The raw payload data

        Returns:
            List of mapped records
        """
        pass

    async def _maybe_flush(self, processed_count: int) -> None:
        """Flush periodically so large runs surface DB errors earlier."""
        if processed_count > 0 and processed_count % self._flush_interval == 0:
            await self.db.flush()

    async def get_or_create_instrument(
        self,
        symbol: str,
        name: Optional[str] = None,
        market: Optional[str] = None,
        asset_class: Optional[str] = None,
    ) -> Instrument:
        """Get existing instrument or create new one."""
        instrument_market = normalize_market(market or self.market)
        effective_asset_class = normalize_asset_class(asset_class or self.asset_class)
        cache_key = (effective_asset_class, instrument_market, symbol)
        cached = self._instrument_cache.get(cache_key)
        if cached is not None:
            return cached

        stmt = select(Instrument).where(
            Instrument.asset_class == effective_asset_class,
            Instrument.market == instrument_market,
            Instrument.symbol == symbol,
        )
        result = await self.db.execute(stmt)
        instrument = result.scalar_one_or_none()

        if instrument:
            self._instrument_cache[cache_key] = instrument
            return instrument

        # Create new instrument
        instrument = Instrument(
            instrument_id=uuid7(),
            asset_class=effective_asset_class,
            market=instrument_market,
            symbol=symbol,
            name=name,
            status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.db.add(instrument)
        await self.db.flush()
        self._instrument_cache[cache_key] = instrument
        return instrument

    async def get_instrument_by_identifier(
        self,
        identifier_type: str,
        identifier_value: str,
        market: Optional[str] = None,
        asset_class: Optional[str] = None,
    ) -> Optional[Instrument]:
        """Resolve instrument using identifier mapping."""
        instrument_market = normalize_market(market or self.market)
        effective_asset_class = normalize_asset_class(asset_class or self.asset_class)
        cache_key = (identifier_type, identifier_value, effective_asset_class, instrument_market)
        if cache_key in self._identifier_cache:
            return self._identifier_cache[cache_key]

        stmt = (
            select(Instrument)
            .join(
                InstrumentIdentifier,
                Instrument.instrument_id == InstrumentIdentifier.instrument_id,
            )
            .where(
                InstrumentIdentifier.id_type == identifier_type,
                InstrumentIdentifier.id_value == identifier_value,
                Instrument.asset_class == effective_asset_class,
                Instrument.market == instrument_market,
            )
        )
        result = await self.db.execute(stmt)
        instrument = result.scalar_one_or_none()
        self._identifier_cache[cache_key] = instrument
        if instrument is not None:
            instrument_cache_key = (
                instrument.asset_class,
                instrument.market,
                instrument.symbol,
            )
            self._instrument_cache[instrument_cache_key] = instrument
        return instrument

    async def ensure_instrument_identifier(
        self,
        instrument_id: UUID,
        identifier_type: str,
        identifier_value: str,
    ) -> None:
        """Ensure instrument identifier mapping exists."""
        cache_key = (identifier_type, identifier_value)
        if cache_key in self._identifier_exists_cache:
            return

        stmt = select(InstrumentIdentifier).where(
            InstrumentIdentifier.id_type == identifier_type,
            InstrumentIdentifier.id_value == identifier_value,
        )
        result = await self.db.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            self._identifier_exists_cache.add(cache_key)
            return

        identifier = InstrumentIdentifier(
            id=uuid7(),
            instrument_id=instrument_id,
            id_type=identifier_type,
            id_value=identifier_value,
            source="normalize",
            created_at=utc_now(),
        )
        self.db.add(identifier)
        await self.db.flush()
        self._identifier_exists_cache.add(cache_key)

    async def resolve_instrument(self, record: InstrumentResolvableRecord) -> Instrument:
        """Resolve instrument by identifier mapping or symbol."""
        record_market = normalize_market(getattr(record, "market", None) or self.market)
        record_asset_class = normalize_asset_class(
            getattr(record, "asset_class", None) or self.asset_class
        )
        if record.identifier_type and record.identifier_value:
            instrument = await self.get_instrument_by_identifier(
                record.identifier_type,
                record.identifier_value,
                market=record_market,
                asset_class=record_asset_class,
            )
            if instrument:
                return instrument

        symbol = record.symbol or record.identifier_value or ""
        instrument = await self.get_or_create_instrument(
            symbol=symbol,
            name=record.name,
            market=record_market,
            asset_class=record_asset_class,
        )

        if record.identifier_type and record.identifier_value:
            await self.ensure_instrument_identifier(
                instrument.instrument_id,
                record.identifier_type,
                record.identifier_value,
            )
            identifier_cache_key = (
                record.identifier_type,
                record.identifier_value,
                record_asset_class,
                record_market,
            )
            self._identifier_cache[identifier_cache_key] = instrument

        return instrument

    async def get_or_create_trading_day(
        self,
        trade_date: datetime | date,
        market: Optional[str] = None,
    ) -> None:
        """Ensure trading calendar entry exists for market/date."""
        trade_date_value = trade_date.date() if isinstance(trade_date, datetime) else trade_date
        calendar_market = normalize_market(market or self.market)
        cache_key = (calendar_market, trade_date_value)
        if cache_key in self._trading_day_cache:
            return

        stmt = select(TradingCalendar).where(
            TradingCalendar.market == calendar_market,
            TradingCalendar.trade_date == trade_date_value,
        )
        result = await self.db.execute(stmt)
        calendar = result.scalar_one_or_none()
        if calendar:
            self._trading_day_cache.add(cache_key)
            return

        # 並行 normalize 多個 chunk 時可能同時嘗試插入相同 (market, trade_date)。
        # 使用 ON CONFLICT DO NOTHING 避免 UniqueViolation 中斷整個 session。
        upsert_stmt = (
            insert(TradingCalendar)
            .values(
                id=uuid7(),
                market=calendar_market,
                trade_date=trade_date_value,
                is_open=True,
            )
            .on_conflict_do_nothing(constraint="uq_calendar")
        )
        await self.db.execute(upsert_stmt)
        self._trading_day_cache.add(cache_key)

    async def upsert_eod(
        self,
        instrument_id: UUID,
        record: MappedRecord,
        run_id: UUID,
    ) -> bool:
        """
        Upsert EOD data record.

        Returns:
            True if successful, False otherwise
        """
        trade_date_value = (
            record.trade_date.date()
            if isinstance(record.trade_date, datetime)
            else record.trade_date
        )
        await self.ensure_eod_partition(trade_date_value)

        stmt = insert(MarketDataEOD).values(
            instrument_id=instrument_id,
            trade_date=trade_date_value,
            open=record.open,
            high=record.high,
            low=record.low,
            close=record.close,
            volume=record.volume,
            total_ticks=record.total_ticks,
            turnover=record.turnover,
            source=record.source,
            asof_ts=utc_now(),
            run_id=run_id,
            created_at=utc_now(),
            updated_at=utc_now(),
        )

        # On conflict, update only when data has actually changed
        t = MarketDataEOD.__table__
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "trade_date"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "total_ticks": stmt.excluded.total_ticks,
                "turnover": stmt.excluded.turnover,
                "source": stmt.excluded.source,
                "asof_ts": stmt.excluded.asof_ts,
                "run_id": stmt.excluded.run_id,
                "updated_at": stmt.excluded.updated_at,
            },
            where=(
                (t.c.open.is_distinct_from(stmt.excluded.open))
                | (t.c.high.is_distinct_from(stmt.excluded.high))
                | (t.c.low.is_distinct_from(stmt.excluded.low))
                | (t.c.close.is_distinct_from(stmt.excluded.close))
                | (t.c.volume.is_distinct_from(stmt.excluded.volume))
                | (t.c.total_ticks.is_distinct_from(stmt.excluded.total_ticks))
                | (t.c.turnover.is_distinct_from(stmt.excluded.turnover))
            ),
        )

        await self.db.execute(stmt)
        # Refresh ORM state for follow-up reads while dropping cached Instrument
        # instances that would otherwise become expired and unsafe to reuse in the
        # remaining async batch.
        self.db.expire_all()
        self._instrument_cache.clear()
        self._identifier_cache.clear()
        return True

    async def ensure_eod_partition(self, trade_date_value: date) -> None:
        """Create the yearly EOD partition before writes reach the default partition."""
        year = trade_date_value.year
        if year in self._eod_partition_cache:
            return
        await self.db.execute(text(f"""
                CREATE TABLE IF NOT EXISTS market_data_eod_y{year}
                PARTITION OF market_data_eod
                FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')
                """))
        self._eod_partition_cache.add(year)

    async def check_duplicate_in_db(
        self,
        instrument_id: UUID,
        trade_date: datetime | date,
    ) -> bool:
        """Check if instrument/trade_date already exists in the database."""
        trade_date_value = trade_date.date() if isinstance(trade_date, datetime) else trade_date
        stmt = select(MarketDataEOD.instrument_id).where(
            MarketDataEOD.instrument_id == instrument_id,
            MarketDataEOD.trade_date == trade_date_value,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none() is not None

    def _check_delisted_instrument(
        self,
        instrument: Instrument,
        trade_date: datetime | date | None,
        raw_data: Optional[dict],
    ) -> Optional[DQIssueRecord]:
        if instrument.status != "delisted":
            return None

        if trade_date is None:
            return DQIssueRecord(
                issue_type="DELISTED_NO_TRADE_DATE",
                severity="error",
                description=f"Instrument {instrument.symbol} is delisted but trade_date is missing",
                raw_data=raw_data,
            )

        trade_date_value = trade_date.date() if isinstance(trade_date, datetime) else trade_date

        if instrument.delisted_date is None:
            return DQIssueRecord(
                issue_type="DELISTED_NO_DATE",
                severity="error",
                description=(
                    f"Instrument {instrument.symbol} is delisted but delisted_date is missing"
                ),
                trade_date=trade_date if isinstance(trade_date, datetime) else None,
                raw_data=raw_data,
            )

        if trade_date_value > instrument.delisted_date:
            return DQIssueRecord(
                issue_type="DELISTED_AFTER_DATE",
                severity="error",
                description=(
                    f"Instrument {instrument.symbol} delisted on {instrument.delisted_date}, "
                    f"trade_date {trade_date_value}"
                ),
                trade_date=trade_date if isinstance(trade_date, datetime) else None,
                raw_data=raw_data,
            )

        return None

    async def record_dq_issue(
        self,
        issue: DQIssueRecord,
        run_id: UUID,
        instrument_id: Optional[UUID] = None,
    ):
        """Record a DQ issue in the database."""
        dq_issue = DQIssue(
            id=uuid7(),
            run_id=run_id,
            instrument_id=instrument_id,
            trade_date=issue.trade_date,
            issue_type=issue.issue_type,
            severity=issue.severity,
            description=issue.description,
            raw_data=issue.raw_data,
            resolved=False,
            created_at=utc_now(),
        )
        self.db.add(dq_issue)

    async def update_run_status(
        self,
        run_id: UUID,
        status: str,
        total: int,
        success: int,
        failed: int,
        error_msg: Optional[str] = None,
    ):
        """Update ingestion run status."""
        stmt = select(IngestionRun).where(IngestionRun.run_id == run_id)
        result = await self.db.execute(stmt)
        run = result.scalar_one_or_none()

        if run:
            run.status = status
            run.total_records = total
            run.success_records = success
            run.failed_records = failed
            run.error_message = error_msg
            if status == "processing" and not run.started_at:
                run.started_at = utc_now()
            if status in ("completed", "completed_with_errors", "failed"):
                run.completed_at = utc_now()

    def _build_processing_error_issue(
        self,
        record: MappedRecord,
        exc: Exception,
    ) -> DQIssueRecord:
        """Convert an unexpected record-level exception into a persisted DQ issue."""
        description = str(exc).strip()
        if description:
            description = f"{type(exc).__name__}: {description}"
        else:
            description = type(exc).__name__

        return DQIssueRecord(
            issue_type="processing_error",
            severity="error",
            description=description,
            trade_date=record.trade_date if isinstance(record.trade_date, datetime) else None,
            raw_data=record.raw_data,
        )

    def _summarize_processing_errors(self, issues: list[DQIssueRecord]) -> Optional[str]:
        """Build a compact run-level summary for unexpected processing errors."""
        processing_errors = [issue for issue in issues if issue.issue_type == "processing_error"]
        if not processing_errors:
            return None

        samples: list[str] = []
        seen: set[str] = set()
        for issue in processing_errors:
            description = issue.description.strip() if issue.description else issue.issue_type
            if description in seen:
                continue
            seen.add(description)
            samples.append(description)
            if len(samples) == 3:
                break

        joined_samples = "; ".join(samples)
        if len(processing_errors) == 1:
            return f"Processing error: {joined_samples}"
        return f"{len(processing_errors)} processing errors. Samples: {joined_samples}"

    async def process(self, raw_payload: dict, run_id: UUID) -> NormalizeResult:
        """
        Main processing method.

        Args:
            raw_payload: The raw data payload
            run_id: The ingestion run ID

        Returns:
            NormalizeResult with processing statistics
        """
        result = NormalizeResult(run_id=run_id)

        try:
            # Update run status to processing
            await self.update_run_status(run_id, "processing", 0, 0, 0)

            # Map fields
            mapped_records = self.map_fields(raw_payload)
            result.total_records = len(mapped_records)

            seen_keys: set[tuple[str, str, datetime]] = set()
            processed_records = 0

            for record in mapped_records:
                instrument: Instrument | None = None
                try:
                    # Validate record
                    issues = self.dq_validator.validate_eod(record, seen_keys=seen_keys)
                    blocking_issues = [i for i in issues if i.severity == "error"]

                    if blocking_issues:
                        # Record DQ issues and skip
                        for issue in issues:
                            await self.record_dq_issue(issue, run_id)
                            result.dq_issues.append(issue)
                        result.failed_records += 1
                        continue

                    # Resolve instrument (identifier mapping or symbol)
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

                    # Ensure trading calendar entry
                    await self.get_or_create_trading_day(record.trade_date, market=record.market)

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

                    record_asset_class = getattr(record, "asset_class", None) or self.asset_class
                    if record_asset_class == "equity":
                        continuity_issue = (
                            await self.dq_validator.check_corporate_action_continuity(
                                self.db,
                                instrument.instrument_id,
                                record,
                            )
                        )
                        if continuity_issue:
                            issues.append(continuity_issue)

                    # Record non-blocking issues
                    for issue in issues:
                        await self.record_dq_issue(
                            issue,
                            run_id,
                            instrument.instrument_id,
                        )
                        result.dq_issues.append(issue)

                    # Upsert EOD data
                    await self.upsert_eod(instrument.instrument_id, record, run_id)
                    result.success_records += 1

                except Exception as e:
                    result.failed_records += 1
                    issue = self._build_processing_error_issue(record, e)
                    await self.record_dq_issue(
                        issue,
                        run_id,
                        instrument.instrument_id if instrument is not None else None,
                    )
                    result.dq_issues.append(issue)
                finally:
                    processed_records += 1
                    await self._maybe_flush(processed_records)

            # Update final status
            status = "completed" if result.failed_records == 0 else "completed_with_errors"
            error_summary = self._summarize_processing_errors(result.dq_issues)
            await self.update_run_status(
                run_id,
                status,
                result.total_records,
                result.success_records,
                result.failed_records,
                error_summary,
            )

            await self.db.commit()

        except Exception as e:
            result.error_message = str(e)
            await self.update_run_status(run_id, "failed", 0, 0, 0, str(e))
            await self.db.commit()

        return result
