"""
Canonical Layer database models.
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Time,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB, NUMERIC
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.utils import utc_now, uuid7
from app.vocabulary import sql_asset_class_check, sql_market_check


class Instrument(Base):
    """
    Instrument master table.
    Stores all tradeable instruments across markets.
    """

    __tablename__ = "instruments"
    __table_args__ = (
        CheckConstraint(
            sql_asset_class_check("asset_class"),
            name="instrument_asset_class_valid",
        ),
        CheckConstraint(sql_market_check("market"), name="instrument_market_valid"),
        UniqueConstraint("asset_class", "market", "symbol", name="uq_instrument"),
        Index("idx_inst_market", "market"),
        Index("idx_inst_status", "status"),
    )

    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    timezone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    listed_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    delisted_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    extra: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    # Relationships
    identifiers: Mapped[list["InstrumentIdentifier"]] = relationship(back_populates="instrument")
    eod_data: Mapped[list["MarketDataEOD"]] = relationship(back_populates="instrument")
    corporate_actions: Mapped[list["CorporateAction"]] = relationship(back_populates="instrument")
    futures_contracts: Mapped[list["FuturesContract"]] = relationship(back_populates="instrument")
    futures_continuous_eod: Mapped[list["FuturesContinuousEOD"]] = relationship(
        back_populates="instrument"
    )
    stats: Mapped[Optional["InstrumentStats"]] = relationship(back_populates="instrument")


class InstrumentStats(Base):
    """Materialized read stats for instrument list/detail endpoints."""

    __tablename__ = "instrument_stats"

    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id", ondelete="CASCADE"),
        primary_key=True,
    )
    first_trade_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    latest_trade_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    latest_price: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    instrument: Mapped["Instrument"] = relationship(back_populates="stats")


class InstrumentIdentifier(Base):
    """
    Instrument identifier mapping table.
    Maps various identifier types (Bloomberg, ISIN, CUSIP, etc.) to instruments.
    """

    __tablename__ = "instrument_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "id_type",
            "id_value",
            "valid_from",
            name="uq_identifier",
            postgresql_nulls_not_distinct=True,
        ),
        Index("idx_ident_inst", "instrument_id"),
        Index("idx_ident_type_value", "id_type", "id_value"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id"),
        nullable=False,
    )
    id_type: Mapped[str] = mapped_column(String(30), nullable=False)
    id_value: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    valid_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    # Relationships
    instrument: Mapped["Instrument"] = relationship(back_populates="identifiers")


class TradingCalendar(Base):
    """
    Trading calendar for different markets.
    """

    __tablename__ = "trading_calendar"
    __table_args__ = (
        CheckConstraint(sql_market_check("market"), name="calendar_market_valid"),
        UniqueConstraint("market", "trade_date", name="uq_calendar"),
        Index("idx_cal_market_date", "market", "trade_date"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)
    session_open: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    session_close: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    holiday_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)


class MarketDataEOD(Base):
    """
    End-of-day market data.
    Stores daily OHLCV data for all instruments.
    """

    __tablename__ = "market_data_eod"
    __table_args__ = (
        Index("idx_eod_date", "trade_date"),
        {"postgresql_partition_by": "RANGE (trade_date)"},
    )

    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id"),
        primary_key=True,
        nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, nullable=False)
    open: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    high: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    low: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    close: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    volume: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    total_ticks: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    turnover: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 4), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    asof_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    # Relationships
    instrument: Mapped["Instrument"] = relationship(back_populates="eod_data")


_CURRENT_YEAR = date.today().year
for _year in range(_CURRENT_YEAR - 5, _CURRENT_YEAR + 6):
    event.listen(
        MarketDataEOD.__table__,
        "after_create",
        DDL(f"""
            CREATE TABLE IF NOT EXISTS market_data_eod_y{_year}
            PARTITION OF market_data_eod
            FOR VALUES FROM ('{_year}-01-01') TO ('{_year + 1}-01-01')
            """),
    )

event.listen(
    MarketDataEOD.__table__,
    "after_create",
    DDL("CREATE TABLE IF NOT EXISTS market_data_eod_default PARTITION OF market_data_eod DEFAULT"),
)


class CorporateAction(Base):
    """
    Corporate actions such as splits and dividends.
    """

    __tablename__ = "corporate_action"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "action_type", "ex_date", name="uq_ca_instrument_action_date"
        ),
        Index("idx_ca_action", "action_type"),
    )

    action_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id"),
        nullable=False,
    )
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    ex_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    record_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    pay_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    ratio: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    cash_amount: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    extra: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    asof_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    instrument: Mapped["Instrument"] = relationship(back_populates="corporate_actions")


class MacroSeries(Base):
    """
    Macro series metadata.
    """

    __tablename__ = "macro_series"
    __table_args__ = (
        CheckConstraint(
            f"market IS NULL OR {sql_market_check('market')}",
            name="macro_series_market_valid",
        ),
        UniqueConstraint("source_code", name="uq_macro_series_source_code"),
        Index("idx_macro_market", "market"),
        Index("idx_macro_source_code", "source_code"),
    )

    series_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    frequency: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    market: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    source_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    observations: Mapped[list["MacroObservation"]] = relationship(back_populates="series")


class MacroObservation(Base):
    """
    Macro series observations.
    """

    __tablename__ = "macro_observation"
    __table_args__ = (
        UniqueConstraint("series_id", "obs_date", name="uq_macro_observation"),
        Index("idx_macro_obs_date", "obs_date"),
        Index("idx_macro_obs_series", "series_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    series_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("macro_series.series_id"),
        nullable=False,
    )
    obs_date: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    asof_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    series: Mapped["MacroSeries"] = relationship(back_populates="observations")


class RollRule(Base):
    """
    Roll rule definition for futures continuous series.
    """

    __tablename__ = "roll_rule"
    __table_args__ = (UniqueConstraint("name", name="uq_roll_rule"),)

    rule_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    continuous_eod: Mapped[list["FuturesContinuousEOD"]] = relationship(back_populates="roll_rule")


class FuturesContract(Base):
    """
    Futures contract metadata.
    """

    __tablename__ = "futures_contract"
    __table_args__ = (
        UniqueConstraint("instrument_id", "contract_code", name="uq_futures_contract"),
        Index("idx_futures_expiry", "expiry_date"),
    )

    contract_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id"),
        nullable=False,
    )
    contract_code: Mapped[str] = mapped_column(String(50), nullable=False)
    contract_month: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    expiry_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    currency: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    extra: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    asof_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    instrument: Mapped["Instrument"] = relationship(back_populates="futures_contracts")


class FuturesContinuousEOD(Base):
    """
    Continuous futures EOD data.
    """

    __tablename__ = "futures_continuous_eod"
    __table_args__ = (
        UniqueConstraint("instrument_id", "trade_date", name="uq_futures_cont_eod"),
        Index("idx_fut_cont_date", "trade_date"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id"),
        nullable=False,
    )
    roll_rule_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("roll_rule.rule_id"),
        nullable=True,
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    high: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    low: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    close: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 8), nullable=True)
    volume: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    turnover: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 4), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    asof_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    instrument: Mapped["Instrument"] = relationship(back_populates="futures_continuous_eod")
    roll_rule: Mapped[Optional["RollRule"]] = relationship(back_populates="continuous_eod")
