"""
Raw Layer database models.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.utils import utc_now


class RawMarketPayload(Base):
    """
    Raw market data payload storage.
    Stores original data from fetch layer before normalization.
    """

    __tablename__ = "market_payload"
    __table_args__ = (
        Index("idx_mp_expire", "expire_at"),
        Index("idx_mp_dataset", "dataset_key"),
        Index("idx_mp_run", "run_id"),
        {"schema": "raw"},
    )

    dataset_key: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    request_key: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    message_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
