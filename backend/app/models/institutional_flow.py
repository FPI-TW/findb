"""Three major institutional investors (三大法人): the exchanges' daily tables.

TWSE and TPEx each publish, per trading day, the market totals of every investor
category in TWD and the per-security flows in shares. A table is stored as the
exchange published it and replaced only by a later fetch of the same table.
Securities are kept by their listed code, warrants included; ``instrument_id``
links a row to an existing TW equity or ETF instrument and stays empty otherwise.

The exchanges' total excludes foreign dealers, whose trades are already counted
under dealers.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import NUMERIC
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.utils import utc_now, uuid7

SHA256_CHECK = r"^[0-9a-f]{64}$"
BOARD_CHECK = "board IN ('TWSE', 'TPEX')"
CATEGORY_CHECK = (
    "category IN ('foreign_ex_dealer', 'foreign_dealer', 'investment_trust', "
    "'dealer_proprietary', 'dealer_hedge', 'foreign_total', 'dealer_total', 'total')"
)
STOCK_FLOW_PREFIXES = (
    "foreign_ex_dealer",
    "foreign_dealer",
    "foreign_total",
    "investment_trust",
    "dealer_proprietary",
    "dealer_hedge",
)


class InstitutionalFlowPublication(Base):
    """The stored version of one exchange table: kind, board and trading day."""

    __tablename__ = "institutional_flow_publication"
    __table_args__ = (
        UniqueConstraint("kind", "board", "trade_date", name="uq_institutional_flow_publication"),
        CheckConstraint("kind IN ('summary', 'stock')", name="kind"),
        CheckConstraint(BOARD_CHECK, name="board"),
        CheckConstraint("row_count >= 1", name="rows"),
        CheckConstraint(
            f"source_raw_sha256 ~ '{SHA256_CHECK}' AND "
            f"(rows_sha256 IS NULL OR rows_sha256 ~ '{SHA256_CHECK}')",
            name="sha256",
        ),
        CheckConstraint(
            "(kind = 'stock') = (rows_sha256 IS NOT NULL)",
            name="rows_object",
        ),
        Index("idx_institutional_flow_publication_date", "trade_date"),
        Index("idx_institutional_flow_publication_run", "run_id"),
    )

    publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    board: Mapped[str] = mapped_column(String(4), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    dataset_key: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_symbol: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    source_fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_raw_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rows_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class InstitutionalFlowSummary(Base):
    """One investor category's TWD totals of a board and day."""

    __tablename__ = "institutional_flow_summary"
    __table_args__ = (
        CheckConstraint(BOARD_CHECK, name="board"),
        CheckConstraint(CATEGORY_CHECK, name="category"),
        CheckConstraint("buy_amount >= 0 AND sell_amount >= 0", name="amounts"),
        CheckConstraint("currency = 'TWD'", name="currency"),
        Index("idx_institutional_flow_summary_publication", "publication_id"),
    )

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    board: Mapped[str] = mapped_column(String(4), primary_key=True)
    category: Mapped[str] = mapped_column(String(30), primary_key=True)
    publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("institutional_flow_publication.publication_id", ondelete="CASCADE"),
        nullable=False,
    )
    buy_amount: Mapped[Decimal] = mapped_column(NUMERIC(20, 0), nullable=False)
    sell_amount: Mapped[Decimal] = mapped_column(NUMERIC(20, 0), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(NUMERIC(20, 0), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="TWD")


class InstitutionalFlowStock(Base):
    """One security's flows by investor category of a board and day, in shares.

    TWSE prints no foreign total and only the net of its dealer total, so those
    columns stay empty for TWSE rows.
    """

    __tablename__ = "institutional_flow_stock"
    __table_args__ = (
        CheckConstraint(BOARD_CHECK, name="board"),
        CheckConstraint("symbol ~ '^[0-9A-Z]{4,6}$'", name="symbol"),
        CheckConstraint(
            " AND ".join(
                f"({prefix}_buy IS NULL OR {prefix}_buy >= 0) AND "
                f"({prefix}_sell IS NULL OR {prefix}_sell >= 0)"
                for prefix in (*STOCK_FLOW_PREFIXES, "dealer_total")
            ),
            name="volumes",
        ),
        CheckConstraint(
            "(foreign_total_buy IS NULL) = (foreign_total_sell IS NULL) AND "
            "(foreign_total_buy IS NULL) = (foreign_total_net IS NULL) AND "
            "(dealer_total_buy IS NULL) = (dealer_total_sell IS NULL)",
            name="subtotals",
        ),
        Index("idx_institutional_flow_stock_symbol", "symbol", "trade_date"),
        Index("idx_institutional_flow_stock_instrument", "instrument_id", "trade_date"),
        Index("idx_institutional_flow_stock_rank", "trade_date", "board", "total_net"),
        Index("idx_institutional_flow_stock_publication", "publication_id"),
    )

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True)
    board: Mapped[str] = mapped_column(String(4), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(6), primary_key=True)
    publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("institutional_flow_publication.publication_id", ondelete="CASCADE"),
        nullable=False,
    )
    instrument_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    foreign_ex_dealer_buy: Mapped[int] = mapped_column(BigInteger, nullable=False)
    foreign_ex_dealer_sell: Mapped[int] = mapped_column(BigInteger, nullable=False)
    foreign_ex_dealer_net: Mapped[int] = mapped_column(BigInteger, nullable=False)
    foreign_dealer_buy: Mapped[int] = mapped_column(BigInteger, nullable=False)
    foreign_dealer_sell: Mapped[int] = mapped_column(BigInteger, nullable=False)
    foreign_dealer_net: Mapped[int] = mapped_column(BigInteger, nullable=False)
    foreign_total_buy: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    foreign_total_sell: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    foreign_total_net: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    investment_trust_buy: Mapped[int] = mapped_column(BigInteger, nullable=False)
    investment_trust_sell: Mapped[int] = mapped_column(BigInteger, nullable=False)
    investment_trust_net: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dealer_proprietary_buy: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dealer_proprietary_sell: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dealer_proprietary_net: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dealer_hedge_buy: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dealer_hedge_sell: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dealer_hedge_net: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dealer_total_buy: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    dealer_total_sell: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    dealer_total_net: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_net: Mapped[int] = mapped_column(BigInteger, nullable=False)
