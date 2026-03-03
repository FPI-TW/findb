"""
System registry and tracking models.
"""

from datetime import datetime
from uuid import UUID
from typing import Optional
from sqlalchemy import String, Text, Boolean, Integer, ForeignKey, Index, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

from app.models.base import Base
from app.utils import utc_now, uuid7


class DatasetRegistry(Base):
    """
    Dataset configuration registry.
    Stores metadata and configuration for each data feed.
    """

    __tablename__ = "dataset_registry"

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


class IngestionRun(Base):
    """
    Ingestion batch run tracking.
    Tracks each data ingestion batch and its status.
    """

    __tablename__ = "ingestion_run"
    __table_args__ = (
        Index("idx_run_dataset", "dataset_key"),
        Index("idx_run_status", "status"),
    )

    run_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    dataset_key: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("dataset_registry.dataset_key"),
        nullable=False,
    )
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    request_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    raw_records: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_records: Mapped[int] = mapped_column(Integer, default=0)
    success_records: Mapped[int] = mapped_column(Integer, default=0)
    failed_records: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    # Relationships
    dataset: Mapped["DatasetRegistry"] = relationship(back_populates="ingestion_runs")
    dq_issues: Mapped[list["DQIssue"]] = relationship(back_populates="run")


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
