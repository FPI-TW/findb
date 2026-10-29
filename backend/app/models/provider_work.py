"""Durable provider FIFO and acceptance fences, independent of scheduler leases."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.utils import utc_now, uuid7


class ProviderWork(Base):
    __tablename__ = "provider_work"
    __table_args__ = (
        UniqueConstraint("environment", "provider", "stable_key", name="uq_provider_work_identity"),
        CheckConstraint(
            "kind IN ('daily', 'historical', 'maintenance', 'rerun')",
            name="provider_work_kind_valid",
        ),
        Index("idx_provider_work_scope", "environment", "provider", "state"),
    )
    work_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    environment: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(50))
    stable_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    dataset_key: Mapped[str] = mapped_column(
        ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT")
    )
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    execution_profile: Mapped[str | None] = mapped_column(String(20), nullable=True)
    scope: Mapped[dict] = mapped_column(JSONB, default=dict)
    manifest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    prepared_deliveries: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    state: Mapped[str] = mapped_column(String(30), default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProviderWorkAttempt(Base):
    __tablename__ = "provider_work_attempt"
    __table_args__ = (
        UniqueConstraint("work_id", "attempt_number", name="uq_provider_work_attempt_round"),
        Index("idx_provider_work_fifo", "state", "queue_order"),
    )
    attempt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    queue_order: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    work_id: Mapped[UUID] = mapped_column(ForeignKey("provider_work.work_id", ondelete="RESTRICT"))
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(30), default="queued")
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    drain_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    proof: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class ProviderWorkOwner(Base):
    """Exactly one durable slot per environment/provider; expiry never releases it."""

    __tablename__ = "provider_work_owner"
    environment: Mapped[str] = mapped_column(String(20), primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), primary_key=True)
    attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_work_attempt.attempt_id", ondelete="RESTRICT"),
        unique=True,
        nullable=True,
    )
    generation: Mapped[int] = mapped_column(BigInteger, default=0)
    token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    host_lock_identity: Mapped[str | None] = mapped_column(String(200), nullable=True)
    recovery_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    acquisition_not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProviderWorkReceipt(Base):
    """A run can never be adopted by a different stable work, including replay."""

    __tablename__ = "provider_work_receipt"
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("ingestion_run.run_id", ondelete="RESTRICT"), primary_key=True
    )
    work_id: Mapped[UUID] = mapped_column(ForeignKey("provider_work.work_id", ondelete="RESTRICT"))
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_work_attempt.attempt_id", ondelete="RESTRICT")
    )
    member_key: Mapped[str] = mapped_column(String(200))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
