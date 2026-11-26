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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.utils import utc_now, uuid7

# The lane every work and owner used before lanes existed; providers that declare
# none run all their work in it.
DEFAULT_LANE = "default"
LANE_PATTERN_CHECK = "{column} ~ '^[a-z][a-z0-9_]{{0,29}}$'"


class ProviderWork(Base):
    __tablename__ = "provider_work"
    __table_args__ = (
        UniqueConstraint("environment", "provider", "stable_key", name="uq_provider_work_identity"),
        CheckConstraint(
            "kind IN ('daily', 'historical', 'maintenance', 'rerun')",
            name="provider_work_kind_valid",
        ),
        CheckConstraint(
            "execution_profile IS NULL OR execution_profile = 'full_market'",
            name="provider_work_profile",
        ),
        CheckConstraint(LANE_PATTERN_CHECK.format(column="lane"), name="provider_work_lane_valid"),
        Index("idx_provider_work_scope", "environment", "provider", "lane", "state"),
    )
    work_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    environment: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(50))
    # The provider's execution lane: each lane has its own FIFO and owner. Fixed at
    # enqueue from the catalog feed (historical work always runs in ``default``).
    lane: Mapped[str] = mapped_column(String(30), default=DEFAULT_LANE, server_default=DEFAULT_LANE)
    stable_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    dataset_key: Mapped[str] = mapped_column(
        ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT")
    )
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    execution_profile: Mapped[str | None] = mapped_column(String(20), nullable=True)
    scope: Mapped[dict] = mapped_column(JSONB, default=dict)
    manifest: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
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
    """Exactly one durable slot per environment/provider/lane; expiry never releases it."""

    __tablename__ = "provider_work_owner"
    __table_args__ = (
        CheckConstraint(
            LANE_PATTERN_CHECK.format(column="lane"), name="provider_work_owner_lane_valid"
        ),
    )
    environment: Mapped[str] = mapped_column(String(20), primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), primary_key=True)
    lane: Mapped[str] = mapped_column(
        String(30), primary_key=True, default=DEFAULT_LANE, server_default=DEFAULT_LANE
    )
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


class ProviderWorkPrepared(Base):
    """One retained delivery per row: binding a new body never rewrites earlier ones.

    ``descriptor`` holds the immutable digests, canonical members, job key and the
    confirmation flag; ``body`` (TOASTed separately) is read only by prepared lookup.
    """

    __tablename__ = "provider_work_prepared"
    work_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_work.work_id", ondelete="RESTRICT"), primary_key=True
    )
    delivery_key: Mapped[str] = mapped_column(Text, primary_key=True)
    descriptor: Mapped[dict] = mapped_column(JSONB)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProviderWorkReceipt(Base):
    """A run can never be adopted by a different stable work, including replay."""

    __tablename__ = "provider_work_receipt"
    # Queue projection and claim ask, per prepared delivery, whether its receipt exists.
    __table_args__ = (Index("idx_provider_work_receipt_member", "work_id", "member_key"),)
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
