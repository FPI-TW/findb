"""
Admin correction audit model.
"""

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.utils import utc_now, uuid7


class CanonicalCorrection(Base):
    """
    Audit trail for every manual correction made to canonical layer records.
    One row is appended per correction action; records are immutable.
    """

    __tablename__ = "canonical_correction"
    __table_args__ = (
        Index("idx_correction_record", "table_name", "record_id"),
        Index("idx_correction_instrument", "instrument_id"),
        Index("idx_correction_created", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    # Which canonical table was corrected (e.g., "market_data_eod", "dq_issue")
    table_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Stable logical record id; no DB FK constraint to survive record deletion.
    record_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # Convenience FK for filtering; SET NULL on instrument delete
    instrument_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id", ondelete="SET NULL"),
        nullable=True,
    )
    trade_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    corrected_by: Mapped[str] = mapped_column(String(200), nullable=False)
    correction_reason: Mapped[str] = mapped_column(Text, nullable=False)
    before_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    after_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
