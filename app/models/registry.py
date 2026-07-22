"""
System registry and tracking models.
"""

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.utils import utc_now, uuid7
from app.vocabulary import sql_asset_class_check, sql_market_check


class DatasetRegistry(Base):
    """
    Dataset configuration registry.
    Stores metadata and configuration for each data feed.
    """

    __tablename__ = "dataset_registry"
    __table_args__ = (
        CheckConstraint(
            sql_asset_class_check("asset_class"),
            name="dataset_registry_asset_class_valid",
        ),
        CheckConstraint(
            sql_market_check("market"),
            name="dataset_registry_market_valid",
        ),
    )

    dataset_key: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    frequency: Mapped[str] = mapped_column(String(20), default="daily")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    # Relationships
    ingestion_runs: Mapped[list["IngestionRun"]] = relationship(back_populates="dataset")


class SourceClient(Base):
    """Authenticated data-provider identity for Source API ingestion."""

    __tablename__ = "source_client"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_source_client_key_hash"),
        Index("idx_source_client_revoked", "revoked_at"),
    )

    client_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_name: Mapped[str] = mapped_column(String(50), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    allowed_datasets: Mapped[Optional[list[str]]] = mapped_column(JSONB, nullable=True)
    rate_limit_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    rate_limit_window: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class IngestionAttempt(Base):
    """Durable audit record for every authenticated canonical ingest attempt."""

    __tablename__ = "ingestion_attempt"
    __table_args__ = (
        CheckConstraint(
            "status IN ('received', 'accepted', 'duplicate', 'rejected', 'aborted')",
            name="ingestion_attempt_status_valid",
        ),
        Index("idx_ingestion_attempt_status_created", "status", "created_at"),
        Index("idx_ingestion_attempt_dataset_created", "dataset_key", "created_at"),
        Index("idx_ingestion_attempt_client_created", "source_client_id", "created_at"),
        Index(
            "idx_ingestion_attempt_idempotency",
            "source_client_id",
            "dataset_key",
            "idempotency_key",
        ),
    )

    attempt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    source_client_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_client.client_id"),
        nullable=True,
    )
    run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    dataset_key: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    schema_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    schema_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    request_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    request_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="received")
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    failure_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class IngestionRun(Base):
    """
    Ingestion batch run tracking.
    Tracks each data ingestion batch and its status.
    """

    __tablename__ = "ingestion_run"
    __table_args__ = (
        Index("idx_run_dataset", "dataset_key"),
        Index("idx_run_status", "status"),
        Index("idx_run_raw_payload", "raw_payload_id"),
        Index(
            "idx_run_delivery_policy_baseline",
            "dataset_key",
            "source",
            "schema_id",
            "schema_version",
            "status",
            "policy_outcome",
            "delivery_mode",
            "is_rerun",
            "batch_data_date",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    dataset_key: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("dataset_registry.dataset_key"),
        nullable=False,
    )
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    source_client_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_client.client_id"),
        nullable=True,
    )
    raw_payload_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("raw.market_payload.raw_payload_id", ondelete="SET NULL"),
        nullable=True,
    )
    request_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    schema_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    schema_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    batch_data_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    delivery_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    policy_outcome: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    policy_details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    is_rerun: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw_records: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_records: Mapped[int] = mapped_column(Integer, default=0)
    success_records: Mapped[int] = mapped_column(Integer, default=0)
    failed_records: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failure_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    # Relationships
    dataset: Mapped["DatasetRegistry"] = relationship(back_populates="ingestion_runs")
    dq_issues: Mapped[list["DQIssue"]] = relationship(back_populates="run")


class MissingDeliveryAlert(Base):
    """Durable, idempotent record of an expected canonical feed not arriving."""

    __tablename__ = "missing_delivery_alert"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'resolved')",
            name="missing_delivery_alert_status_valid",
        ),
        UniqueConstraint(
            "dataset_key",
            "source",
            "schema_id",
            "schema_version",
            "expected_data_date",
            name="uq_missing_delivery_identity_date",
        ),
        Index("idx_missing_delivery_status_detected", "status", "first_detected_at"),
        Index("idx_missing_delivery_dataset_source", "dataset_key", "source"),
    )

    alert_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    dataset_key: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    schema_id: Mapped[str] = mapped_column(String(50), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_data_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="open")
    first_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class NormalizationJob(Base):
    """Durable normalization control state; RabbitMQ is only the delivery layer."""

    __tablename__ = "normalization_job"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_normalization_job_run"),
        Index("idx_normalization_job_state_available", "status", "available_at"),
        Index("idx_normalization_job_dataset", "dataset_key"),
    )

    job_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    dataset_key: Mapped[str] = mapped_column(
        String(50), ForeignKey("dataset_registry.dataset_key"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="queued")
    delivery_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, default=uuid7)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class NormalizationOutbox(Base):
    """Transactional outbox event for publishing a normalization task."""

    __tablename__ = "normalization_outbox"
    __table_args__ = (
        Index("idx_normalization_outbox_pending", "status", "available_at"),
        Index("idx_normalization_outbox_run", "run_id"),
    )

    outbox_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("normalization_job.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    delivery_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, default="normalize_run")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    claim_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class NormalizationWorkerHeartbeat(Base):
    """Last-seen state for an independently running normalization worker."""

    __tablename__ = "normalization_worker_heartbeat"

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    current_run_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DQIssue(Base):
    """
    Data quality issue tracking.
    Records all DQ issues encountered during normalization.
    """

    __tablename__ = "dq_issue"
    __table_args__ = (
        Index("idx_dq_run", "run_id"),
        Index("idx_dq_resolved", "resolved"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id"),
        nullable=True,
    )
    instrument_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id"),
        nullable=True,
    )
    trade_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    issue_type: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    # Relationships
    run: Mapped[Optional["IngestionRun"]] = relationship(back_populates="dq_issues")


class APIKey(Base):
    """Hashed API keys for Serve API consumers."""

    __tablename__ = "api_key"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_api_key_hash"),
        Index("idx_api_key_revoked", "revoked_at"),
        Index("idx_api_key_owner", "owner"),
    )

    key_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    tier: Mapped[str] = mapped_column(String(30), nullable=False, default="standard")
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    rate_limit_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    rate_limit_window: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    page_size_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
