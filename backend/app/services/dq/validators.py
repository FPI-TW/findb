"""
Data Quality validators.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import CorporateAction, MarketDataEOD
from app.services.normalize.types import EODLikeRecord, MarketMinuteRecord


@dataclass
class DQIssueRecord:
    """Data quality issue record."""

    issue_type: str
    severity: str  # info, warning, error, critical
    description: str
    trade_date: Optional[datetime] = None
    raw_data: Optional[dict] = None
    instrument_id: Optional[str] = None


class DQValidator:
    """
    Data Quality validator.
    Implements various validation rules for market data.
    """

    # Abnormal return threshold (30%)
    ABNORMAL_RETURN_THRESHOLD = Decimal("0.30")
    # Corporate action continuity tolerance (10%)
    CORP_ACTION_TOLERANCE = Decimal("0.10")

    def validate_eod(
        self,
        record: EODLikeRecord,
        *,
        seen_keys: Optional[set[tuple[str, str, datetime]]] = None,
    ) -> list[DQIssueRecord]:
        """
        Validate an EOD record.

        Args:
            record: The mapped record to validate

        Returns:
            List of DQ issues found
        """
        issues = []

        # DUPLICATE_KEY: instrument_id + trade_date not duplicated in batch
        issue = self._check_duplicate_key(record, seen_keys)
        if issue:
            issues.append(issue)

        # OHLC_HIGH_CHECK: high >= max(open, close)
        issue = self._check_ohlc_high(record)
        if issue:
            issues.append(issue)

        # OHLC_LOW_CHECK: low <= min(open, close)
        issue = self._check_ohlc_low(record)
        if issue:
            issues.append(issue)

        # VOLUME_POSITIVE: volume >= 0
        issue = self._check_volume_positive(record)
        if issue:
            issues.append(issue)

        # MISSING_OHLC: Check for null OHLC values
        issue = self._check_missing_ohlc(record)
        if issue:
            issues.append(issue)

        # ABNORMAL_RETURN: Single day return > 30%
        issue = self._check_abnormal_return(record)
        if issue:
            issues.append(issue)

        return issues

    def validate_minute(
        self,
        record: MarketMinuteRecord,
        *,
        seen_keys: set[tuple[str, str, datetime]],
        anomaly_fields: set[str],
    ) -> list[DQIssueRecord]:
        """Validate minute-bar invariants independently from ingress validation."""
        issues: list[DQIssueRecord] = []
        trade_timestamp = record.bar_start_time

        def error(issue_type: str, description: str) -> None:
            issues.append(
                DQIssueRecord(
                    issue_type=issue_type,
                    severity="error",
                    description=description,
                    trade_date=trade_timestamp,
                    raw_data=record.raw_data,
                )
            )

        if (
            record.bar_start_time is None
            or record.bar_end_time is None
            or record.signal_time is None
        ):
            error("MINUTE_TIMESTAMP_INVALID", "bar timestamps must be timezone-aware UTC datetimes")
        else:
            if any(
                value.tzinfo is None or value.utcoffset() is None
                for value in (record.bar_start_time, record.bar_end_time, record.signal_time)
            ):
                error(
                    "MINUTE_TIMESTAMP_INVALID",
                    "bar timestamps must be timezone-aware UTC datetimes",
                )
            elif record.bar_end_time - record.bar_start_time != timedelta(minutes=1):
                error(
                    "MINUTE_INTERVAL_INVALID",
                    "bar_end_time must be exactly one minute after bar_start_time",
                )
            elif record.signal_time != record.bar_end_time:
                error("MINUTE_SIGNAL_INVALID", "signal_time must equal bar_end_time")

        if record.market_timezone != "Asia/Taipei":
            error("MINUTE_MARKET_TIMEZONE_INVALID", "market_timezone must be Asia/Taipei")
        if record.price_adjustment != "none":
            error("MINUTE_PRICE_ADJUSTMENT_INVALID", "price_adjustment must be none")
        if not record.source:
            error("MINUTE_SOURCE_INVALID", "contract ingestion source is required")
        if record.trade_count is not None:
            error("MINUTE_TRADE_COUNT_INVALID", "trade_count must be null")
        if record.bar_start_time is not None:
            taipei_date = record.bar_start_time.astimezone(ZoneInfo("Asia/Taipei")).date()
            if record.trade_date != taipei_date:
                error(
                    "MINUTE_TRADE_DATE_INVALID",
                    "trade_date must be the Taiwan-local bar_start_time date",
                )
            key = (str(record.market or "").upper(), record.symbol, record.bar_start_time)
            if key in seen_keys:
                error("DUPLICATE_KEY", "Duplicate instrument/bar_start_time in batch")
            seen_keys.add(key)

        if any(value is None for value in (record.open, record.high, record.low, record.close)):
            error("MISSING_OHLC", "minute OHLC fields are required")
        else:
            assert record.open is not None
            assert record.high is not None
            assert record.low is not None
            assert record.close is not None
            if record.high < max(record.open, record.close):
                error("OHLC_HIGH_CHECK", "High must be at least max(open, close)")
            if record.low > min(record.open, record.close):
                error("OHLC_LOW_CHECK", "Low must be at most min(open, close)")
            if any(value < 0 for value in (record.open, record.high, record.low, record.close)):
                error("MINUTE_OHLC_NEGATIVE", "minute OHLC fields must be nonnegative")
        if record.volume is not None and record.volume < 0:
            error("VOLUME_POSITIVE", "Volume must be non-negative")
        if record.turnover is not None and record.turnover < 0:
            error("TURNOVER_POSITIVE", "Turnover must be non-negative")
        for field, value in (("volume", record.volume), ("turnover", record.turnover)):
            if value is None and field not in anomaly_fields:
                error(
                    "MINUTE_NULL_WITHOUT_ANOMALY", f"null {field} requires adapter anomaly evidence"
                )
        return issues

    def _check_ohlc_high(self, record: EODLikeRecord) -> Optional[DQIssueRecord]:
        """Check if high >= max(open, close)."""
        if record.high is None or record.open is None or record.close is None:
            return None

        max_oc = max(record.open, record.close)
        if record.high < max_oc:
            return DQIssueRecord(
                issue_type="OHLC_HIGH_CHECK",
                severity="error",
                description=f"High ({record.high}) is less than max(open, close) ({max_oc})",
                trade_date=record.trade_date if isinstance(record.trade_date, datetime) else None,
                raw_data=record.raw_data,
            )
        return None

    def _check_ohlc_low(self, record: EODLikeRecord) -> Optional[DQIssueRecord]:
        """Check if low <= min(open, close)."""
        if record.low is None or record.open is None or record.close is None:
            return None

        min_oc = min(record.open, record.close)
        if record.low > min_oc:
            return DQIssueRecord(
                issue_type="OHLC_LOW_CHECK",
                severity="error",
                description=f"Low ({record.low}) is greater than min(open, close) ({min_oc})",
                trade_date=record.trade_date if isinstance(record.trade_date, datetime) else None,
                raw_data=record.raw_data,
            )
        return None

    def _check_volume_positive(self, record: EODLikeRecord) -> Optional[DQIssueRecord]:
        """Check if volume is non-negative."""
        if record.volume is None:
            return None

        if record.volume < 0:
            return DQIssueRecord(
                issue_type="VOLUME_POSITIVE",
                severity="error",
                description=f"Volume ({record.volume}) is negative",
                trade_date=record.trade_date if isinstance(record.trade_date, datetime) else None,
                raw_data=record.raw_data,
            )
        return None

    def _check_missing_ohlc(self, record: EODLikeRecord) -> Optional[DQIssueRecord]:
        """Check for missing OHLC values."""
        missing = []
        if record.open is None:
            missing.append("open")
        if record.high is None:
            missing.append("high")
        if record.low is None:
            missing.append("low")
        if record.close is None:
            missing.append("close")

        if missing:
            return DQIssueRecord(
                issue_type="MISSING_OHLC",
                severity="warning",
                description=f"Missing OHLC fields: {', '.join(missing)}",
                trade_date=record.trade_date if isinstance(record.trade_date, datetime) else None,
                raw_data=record.raw_data,
            )
        return None

    def _check_abnormal_return(self, record: EODLikeRecord) -> Optional[DQIssueRecord]:
        """Check for abnormal single-day return (>30%)."""
        if record.open is None or record.close is None or record.open == 0:
            return None

        daily_return = abs((record.close - record.open) / record.open)

        if daily_return > self.ABNORMAL_RETURN_THRESHOLD:
            return DQIssueRecord(
                issue_type="ABNORMAL_RETURN",
                severity="warning",
                description=f"Abnormal daily return: {daily_return:.2%} (threshold: {self.ABNORMAL_RETURN_THRESHOLD:.0%})",
                trade_date=record.trade_date if isinstance(record.trade_date, datetime) else None,
                raw_data=record.raw_data,
            )
        return None

    def _check_duplicate_key(
        self,
        record: EODLikeRecord,
        seen_keys: Optional[set[tuple[str, str, datetime]]],
    ) -> Optional[DQIssueRecord]:
        """Check for duplicate instrument/trade_date within the batch."""
        if seen_keys is None:
            return None
        if not record.symbol or not record.trade_date:
            return None

        market = getattr(record, "market", None)
        market_key = str(market).upper().strip() if market else ""
        key = (market_key, record.symbol, record.trade_date)
        if key in seen_keys:
            return DQIssueRecord(
                issue_type="DUPLICATE_KEY",
                severity="error",
                description="Duplicate instrument/trade_date in batch",
                trade_date=record.trade_date if isinstance(record.trade_date, datetime) else None,
                raw_data=record.raw_data,
            )

        seen_keys.add(key)
        return None

    async def check_corporate_action_continuity(
        self,
        db: AsyncSession,
        instrument_id: UUID,
        record: EODLikeRecord,
    ) -> Optional[DQIssueRecord]:
        """Check price continuity around corporate actions (equity only)."""
        if record.trade_date is None:
            return None
        actual_price = record.close if record.close is not None else record.open
        if actual_price is None:
            return None

        trade_date_value = (
            record.trade_date.date()
            if isinstance(record.trade_date, datetime)
            else record.trade_date
        )

        action_stmt = select(CorporateAction).where(
            CorporateAction.instrument_id == instrument_id,
            CorporateAction.ex_date == trade_date_value,
        )
        action_result = await db.execute(action_stmt)
        actions = list(action_result.scalars().all())
        if not actions:
            return None

        prev_stmt = (
            select(MarketDataEOD)
            .where(
                MarketDataEOD.instrument_id == instrument_id,
                MarketDataEOD.trade_date < trade_date_value,
            )
            .order_by(MarketDataEOD.trade_date.desc())
            .limit(1)
        )
        prev_result = await db.execute(prev_stmt)
        prev_eod = prev_result.scalar_one_or_none()
        if not prev_eod or prev_eod.close is None:
            return None

        ratio = Decimal("1")
        cash_amount = Decimal("0")
        for action in actions:
            if action.ratio is not None and action.ratio > 0:
                ratio *= action.ratio
            if action.cash_amount is not None:
                cash_amount += action.cash_amount

        if ratio == 1 and cash_amount == 0:
            return None

        try:
            expected_close = prev_eod.close / ratio if ratio != 0 else prev_eod.close
        except (ArithmeticError, ZeroDivisionError):
            return None

        expected_close = expected_close - cash_amount
        if expected_close <= 0:
            return None

        deviation = abs(actual_price - expected_close) / expected_close
        if deviation <= self.CORP_ACTION_TOLERANCE:
            return None

        return DQIssueRecord(
            issue_type="CORP_ACTION_CONTINUITY",
            severity="warning",
            description=(
                "Corporate action continuity check failed: "
                f"expected {expected_close} based on ex-date actions, "
                f"actual {actual_price}, deviation {float(deviation):.2%}"
            ),
            trade_date=record.trade_date if isinstance(record.trade_date, datetime) else None,
            raw_data=record.raw_data,
        )
