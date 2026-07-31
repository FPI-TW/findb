"""
System registry and tracking models.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import (
    BigInteger,
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, NUMERIC
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


class TWMinuteUniverseRelease(Base):
    """Versioned maintained TW minute universe; members are never deleted."""

    __tablename__ = "tw_minute_universe_release"
    __table_args__ = (
        UniqueConstraint(
            "dataset_key", "effective_date", "version", name="uq_tw_minute_universe_release"
        ),
        CheckConstraint(
            "status IN ('candidate', 'published', 'superseded')",
            name="tw_minute_universe_release_status_valid",
        ),
        CheckConstraint(
            "change_ratio >= 0 AND change_ratio <= 1", name="tw_minute_universe_change_ratio"
        ),
        CheckConstraint("version >= 1", name="tw_minute_universe_version_positive"),
        CheckConstraint(
            "previous_member_count IS NULL OR previous_member_count >= 0",
            name="tw_minute_universe_previous_count_nonnegative",
        ),
        CheckConstraint("member_count >= 0", name="tw_minute_universe_member_count_nonnegative"),
        CheckConstraint(
            "member_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_universe_member_checksum_valid",
        ),
        CheckConstraint("change_count >= 0", name="tw_minute_universe_change_count_nonnegative"),
        CheckConstraint(
            "change_count_threshold = 20", name="tw_minute_universe_count_threshold_fixed"
        ),
        CheckConstraint(
            "change_ratio_threshold = 0.02", name="tw_minute_universe_ratio_threshold_fixed"
        ),
        CheckConstraint(
            "status != 'published' OR COALESCE("
            "jsonb_typeof(shioaji_eligibility_snapshot) = 'object' "
            "AND shioaji_eligibility_snapshot <> '{}'::jsonb "
            "AND shioaji_eligibility_snapshot->>'source' = 'shioaji' "
            "AND shioaji_eligibility_snapshot ? 'eligibility' "
            "AND jsonb_typeof(shioaji_eligibility_snapshot->'eligibility') IN ('array', 'object') "
            "AND shioaji_eligibility_snapshot->'eligibility' NOT IN ('[]'::jsonb, '{}'::jsonb) "
            "AND jsonb_typeof(official_membership_snapshot) = 'object' "
            "AND official_membership_snapshot <> '{}'::jsonb "
            "AND official_membership_snapshot->>'source' IN ('twse', 'tpex', 'twse_tpex') "
            "AND official_membership_snapshot ? 'membership' "
            "AND official_membership_snapshot ? 'classification' "
            "AND jsonb_typeof(official_membership_snapshot->'membership') IN ('array', 'object') "
            "AND official_membership_snapshot->'membership' NOT IN ('[]'::jsonb, '{}'::jsonb) "
            "AND jsonb_typeof(official_membership_snapshot->'classification') IN ('array', 'object') "
            "AND official_membership_snapshot->'classification' NOT IN ('[]'::jsonb, '{}'::jsonb) "
            "AND shioaji_eligibility_checksum ~ '^[0-9a-f]{64}$' "
            "AND official_membership_checksum ~ '^[0-9a-f]{64}$' "
            "AND shioaji_eligibility_checksum <> official_membership_checksum "
            "AND jsonb_typeof(change_audit) = 'object' AND change_audit <> '{}'::jsonb "
            "AND change_audit ? 'decision' AND change_audit ? 'differences' "
            "AND published_at IS NOT NULL, FALSE)",
            name="tw_minute_universe_published_evidence",
        ),
        CheckConstraint(
            "status != 'published' OR (change_count <= 20 AND change_ratio <= 0.02)",
            name="tw_minute_universe_published_within_thresholds",
        ),
        Index("idx_tw_minute_universe_release_lookup", "dataset_key", "effective_date", "status"),
    )

    release_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    dataset_key: Mapped[str] = mapped_column(
        String(50), ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT"), nullable=False
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="candidate")
    shioaji_eligibility_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    shioaji_eligibility_checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    official_membership_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    official_membership_checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    member_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_member_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    change_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    change_ratio: Mapped[Decimal] = mapped_column(NUMERIC(8, 6), nullable=False, default=0)
    change_count_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    change_ratio_threshold: Mapped[Decimal] = mapped_column(
        NUMERIC(8, 6), nullable=False, default=Decimal("0.02")
    )
    change_audit: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    members: Mapped[list["TWMinuteUniverseMember"]] = relationship(back_populates="release")


class TWMinuteUniverseMember(Base):
    """Historical membership closure is represented by ``ended_on``, never deletion."""

    __tablename__ = "tw_minute_universe_member"
    __table_args__ = (
        UniqueConstraint("release_id", "instrument_id", name="uq_tw_minute_universe_member"),
        CheckConstraint(
            "ended_on IS NULL OR ended_on >= started_on", name="tw_minute_member_date_order"
        ),
        Index("idx_tw_minute_universe_member_instrument", "instrument_id", "ended_on"),
    )

    member_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tw_minute_universe_release.release_id", ondelete="RESTRICT"),
        nullable=False,
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    ended_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    source_symbol: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    release: Mapped["TWMinuteUniverseRelease"] = relationship(back_populates="members")


class TWMinuteDailyUpdate(Base):
    """One durable update control row per environment, market, and local trade date."""

    __tablename__ = "tw_minute_daily_update"
    __table_args__ = (
        UniqueConstraint("environment", "market", "trade_date", name="uq_tw_minute_daily_update"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'blocked', 'skipped_calendar')",
            name="tw_minute_daily_update_status_valid",
        ),
        CheckConstraint("market = 'TW'", name="tw_minute_daily_update_market_tw"),
        Index("idx_tw_minute_daily_update_status", "status", "trade_date"),
    )

    update_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    environment: Mapped[str] = mapped_column(String(30), nullable=False)
    market: Mapped[str] = mapped_column(String(10), nullable=False, default="TW")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class TWMinuteDatasetSnapshot(Base):
    """Deterministic request plan for one dataset, date, and universe release."""

    __tablename__ = "tw_minute_dataset_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "daily_update_id", "dataset_key", name="uq_tw_minute_snapshot_daily_dataset"
        ),
        UniqueConstraint(
            "daily_update_id",
            "dataset_key",
            "trade_date",
            "universe_release_id",
            "symbols_checksum",
            name="uq_tw_minute_snapshot_identity",
        ),
        CheckConstraint(
            "status IN ('planned', 'running', 'completed', 'failed', 'partial')",
            name="tw_minute_snapshot_status_valid",
        ),
        CheckConstraint(
            "sequence_count >= 1 AND expected_rows >= 0 AND received_rows >= 0",
            name="tw_minute_snapshot_counts_nonnegative",
        ),
        Index("idx_tw_minute_snapshot_update", "daily_update_id", "dataset_key"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    daily_update_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tw_minute_daily_update.update_id", ondelete="RESTRICT"),
        nullable=False,
    )
    dataset_key: Mapped[str] = mapped_column(
        String(50), ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT"), nullable=False
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    universe_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tw_minute_universe_release.release_id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbols_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")
    symbol_results: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class TWMinuteSnapshotPart(Base):
    """A bounded request sequence and its optional Source API ingestion run."""

    __tablename__ = "tw_minute_snapshot_part"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "sequence", name="uq_tw_minute_snapshot_part_sequence"),
        CheckConstraint("sequence >= 1", name="tw_minute_snapshot_part_sequence_positive"),
        CheckConstraint(
            "request_count >= 0 AND received_rows >= 0",
            name="tw_minute_snapshot_part_counts_nonnegative",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="tw_minute_snapshot_part_status_valid",
        ),
    )

    part_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tw_minute_dataset_snapshot.snapshot_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    ingestion_run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    symbols: Mapped[Optional[list[str]]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    received_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_summary: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class TWMinutePublicationRevision(Base):
    """Immutable publication manifest; service code may later promote one to latest."""

    __tablename__ = "tw_minute_publication_revision"
    __table_args__ = (
        UniqueConstraint("daily_update_id", "revision", name="uq_tw_minute_publication_revision"),
        CheckConstraint(
            "status IN ('completed', 'completed_with_warnings', 'unresolved_gap')",
            name="tw_minute_publication_status_valid",
        ),
        CheckConstraint("revision >= 1", name="tw_minute_publication_revision_positive"),
        CheckConstraint(
            "NOT (is_latest AND status = 'unresolved_gap')",
            name="tw_minute_publication_latest_resolved",
        ),
        CheckConstraint(
            "(status = 'completed_with_warnings' AND warning_kind IS NOT NULL "
            "AND warning_kind = 'expected_no_data') "
            "OR (status != 'completed_with_warnings' AND warning_kind IS NULL)",
            name="tw_minute_publication_warning_kind_coherent",
        ),
        CheckConstraint(
            "status != 'completed_with_warnings' OR COALESCE("
            "jsonb_typeof(manifest) = 'object' "
            "AND jsonb_typeof(manifest->'expected_no_data_symbols') = 'array' "
            "AND jsonb_array_length(manifest->'expected_no_data_symbols') > 0, FALSE)",
            name="tw_minute_publication_warning_manifest_evidence",
        ),
        CheckConstraint(
            "NOT is_latest OR published_at IS NOT NULL",
            name="tw_minute_publication_latest_published",
        ),
        Index(
            "uq_tw_minute_publication_latest",
            "daily_update_id",
            unique=True,
            postgresql_where=text("is_latest"),
        ),
    )

    publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    daily_update_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tw_minute_daily_update.update_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    manifest_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    warning_kind: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TWMinuteArchiveRelease(Base):
    """Permanent metadata for atomically finalized recorder archive imports."""

    __tablename__ = "tw_minute_archive_release"
    __table_args__ = (
        UniqueConstraint(
            "dataset_key", "source", "manifest_checksum", name="uq_tw_minute_archive_manifest"
        ),
        CheckConstraint("source = 'tw_recorder_archive'", name="tw_minute_archive_source_valid"),
        CheckConstraint(
            "upstream_source = 'shioaji'", name="tw_minute_archive_upstream_source_valid"
        ),
        CheckConstraint(
            "overlap_precedence = 'direct_daily_shioaji'",
            name="tw_minute_archive_overlap_precedence_valid",
        ),
        CheckConstraint(
            "trading_calendar_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_calendar_checksum_valid",
        ),
        CheckConstraint(
            "release_key ~ '^[a-z0-9][a-z0-9._-]*$'",
            name="tw_minute_archive_release_key_valid",
        ),
        CheckConstraint(
            "content_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_content_checksum_valid",
        ),
        CheckConstraint(
            "manifest_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_manifest_checksum_valid",
        ),
        CheckConstraint("coverage_start <= coverage_end", name="tw_minute_archive_coverage_order"),
        CheckConstraint(
            "status IN ('staged', 'finalized', 'failed')", name="tw_minute_archive_status_valid"
        ),
        CheckConstraint(
            "instrument_count >= 1 AND row_count >= 1 AND sequence_count >= 1",
            name="tw_minute_archive_counts_positive",
        ),
        CheckConstraint(
            "(status = 'finalized') = (finalized_at IS NOT NULL)",
            name="tw_minute_archive_finalized_timestamp",
        ),
        CheckConstraint(
            "instruments_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_instruments_checksum_valid",
        ),
        CheckConstraint(
            "trading_dates_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_dates_checksum_valid",
        ),
        CheckConstraint(
            "expected_trading_date_count >= 1 AND covered_trading_date_count >= 1 AND chunk_count >= 1",
            name="tw_minute_archive_manifest_counts_positive",
        ),
    )

    release_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    release_key: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_key: Mapped[str] = mapped_column(
        String(50), ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="tw_recorder_archive")
    upstream_source: Mapped[str] = mapped_column(String(50), nullable=False, default="shioaji")
    overlap_precedence: Mapped[str] = mapped_column(
        String(50), nullable=False, default="direct_daily_shioaji"
    )
    trading_calendar_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    instruments_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_dates_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_trading_date_count: Mapped[int] = mapped_column(Integer, nullable=False)
    covered_trading_date_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_start: Mapped[date] = mapped_column(Date, nullable=False)
    coverage_end: Mapped[date] = mapped_column(Date, nullable=False)
    instrument_count: Mapped[int] = mapped_column(Integer, nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sequence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    content_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="staged")
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TWMinuteArchiveChunk(Base):
    """Contiguous monthly archive chunk belonging to one immutable release."""

    __tablename__ = "tw_minute_archive_chunk"
    __table_args__ = (
        UniqueConstraint(
            "release_id",
            "snapshot_sequence",
            "chunk_sequence",
            name="uq_tw_minute_archive_chunk_sequence",
        ),
        CheckConstraint(
            "snapshot_sequence >= 1 AND chunk_sequence >= 1 AND chunk_sequence <= chunk_count",
            name="tw_minute_archive_chunk_sequence_valid",
        ),
        CheckConstraint(
            "instrument_count >= 1", name="tw_minute_archive_chunk_instrument_positive"
        ),
        CheckConstraint(
            "row_count >= 1 AND row_count <= 5000", name="tw_minute_archive_chunk_row_count_range"
        ),
        CheckConstraint(
            "object_size_bytes >= 1", name="tw_minute_archive_chunk_object_size_positive"
        ),
        CheckConstraint(
            "chunk_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_chunk_checksum_valid",
        ),
        CheckConstraint(
            "object_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_chunk_object_checksum_valid",
        ),
        CheckConstraint(
            "instrument_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_chunk_instrument_checksum_valid",
        ),
        CheckConstraint(
            "coverage_start <= coverage_end", name="tw_minute_archive_chunk_coverage_order"
        ),
        CheckConstraint(
            "date_trunc('month', coverage_start::timestamp)::date = coverage_start",
            name="tw_minute_archive_chunk_start_month",
        ),
        CheckConstraint(
            "date_trunc('month', coverage_end::timestamp)::date = coverage_end",
            name="tw_minute_archive_chunk_end_month",
        ),
    )

    chunk_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tw_minute_archive_release.release_id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_start: Mapped[date] = mapped_column(Date, nullable=False)
    coverage_end: Mapped[date] = mapped_column(Date, nullable=False)
    instrument_count: Mapped[int] = mapped_column(Integer, nullable=False)
    instrument_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    object_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SourceClient(Base):
    """Authenticated data-provider identity for Source API ingestion."""

    __tablename__ = "source_client"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_source_client_key_hash"),
        Index("idx_source_client_revoked", "revoked_at"),
    )

    client_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    owner: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_name: Mapped[str] = mapped_column(String(50), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    allowed_datasets: Mapped[Optional[list[str]]] = mapped_column(JSONB, nullable=True)
    rate_limit_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    rate_limit_window: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_from_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("source_client.client_id"), nullable=True
    )
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


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
            name="status_valid",
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
        Index(
            "uq_normalization_outbox_active_job",
            "job_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'publishing')"),
        ),
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
        CheckConstraint("kind IN ('serve', 'admin')", name="api_key_kind_valid"),
        CheckConstraint(
            "role IS NULL OR role IN ('owner', 'operator', 'viewer')",
            name="api_key_role_valid",
        ),
    )

    key_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="serve")
    name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    role: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    tier: Mapped[str] = mapped_column(String(30), nullable=False, default="standard")
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    rate_limit_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    rate_limit_window: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    page_size_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_from_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("api_key.key_id"), nullable=True
    )


class AdminUser(Base):
    """Named human administrator authenticated with a password and session."""

    __tablename__ = "admin_user"
    __table_args__ = (
        UniqueConstraint("username", name="uq_admin_user_username"),
        Index("idx_admin_user_role_active", "role", "is_active"),
        CheckConstraint("role IN ('owner', 'operator', 'viewer')", name="admin_user_role_valid"),
    )

    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    disabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AdminSession(Base):
    """Revocable, hashed opaque session for an Admin user."""

    __tablename__ = "admin_session"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_admin_session_token_hash"),
        Index("idx_admin_session_user_active", "user_id", "revoked_at"),
        Index("idx_admin_session_expires", "expires_at"),
    )

    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_user.user_id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class CredentialUsageRollup(Base):
    """Near-real-time aggregate usage for DB-backed credentials."""

    __tablename__ = "credential_usage_rollup"
    __table_args__ = (
        UniqueConstraint("credential_kind", "credential_id", name="uq_credential_usage_ref"),
    )

    usage_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    credential_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    credential_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_endpoint: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AdminAuditEvent(Base):
    """Immutable attribution record for Admin identity and credential mutations."""

    __tablename__ = "admin_audit_event"
    __table_args__ = (
        Index("idx_admin_audit_event_created", "created_at"),
        Index("idx_admin_audit_event_resource", "resource_type", "resource_id"),
    )

    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    actor_display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(100), nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
