"""
Raw Layer database models.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.utils import utc_now, uuid7


class RawMarketPayload(Base):
    """
    Raw market data payload storage.
    Stores original data from fetch layer before normalization.
    """

    __tablename__ = "market_payload"
    __table_args__ = (
        UniqueConstraint(
            "source_client_id",
            "dataset_key",
            "idempotency_key",
            name="uq_raw_payload_idempotency_scope",
            postgresql_nulls_not_distinct=True,
        ),
        Index("idx_mp_expire", "expire_at"),
        Index("idx_mp_dataset", "dataset_key"),
        Index("idx_mp_run", "run_id"),
        {"schema": "raw"},
    )

    raw_payload_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    source_client_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_client.client_id"),
        nullable=True,
    )
    dataset_key: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    request_key: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
