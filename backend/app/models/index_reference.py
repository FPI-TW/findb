"""Index reference data: definitions, published changes, snapshots and membership.

An index publisher (Hang Seng Indexes Company, ``hsil``) publishes three kinds
of document, each kept here as published:

- a constituent report per trading day: every constituent with its weight;
- a change workbook per index family: every effective addition and deletion;
- an index review release: changes announced before they take effect.

``index_membership`` is derived from them and is never written by a delivery:
the latest snapshot anchors it and the published changes roll it forwards and
backwards.  Industry classification is membership of an industry index, so it
has no table of its own.  Symbols are kept as published; ``instrument_id`` is
resolved per membership interval and may stay empty, because exchange codes
are reused over time.
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import NUMERIC
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.utils import utc_now, uuid7

INDEX_CODE_CHECK = r"^[0-9]{5}\.[0-9]{2}$"
SYMBOL_CHECK = r"^[0-9]{5}$"
SHA256_CHECK = r"^[0-9a-f]{64}$"


class ReferenceDocumentArchive(Base):
    """One publisher document kept for good in the canonical bucket.

    The publisher deletes its daily reports after about 36 trading days, so the
    document a delivery was read from is copied here once, as the gzip the
    producer stored.  The object key names the gzip's own content, so a stored
    object never changes; a republished document is a new row and object.
    The publisher's download service sends no ETag and often no Last-Modified.
    """

    __tablename__ = "reference_document_archive"
    __table_args__ = (
        UniqueConstraint("gzip_sha256", name="uq_reference_document_archive_gzip"),
        UniqueConstraint("object_key", name="uq_reference_document_archive_key"),
        CheckConstraint(
            f"gzip_sha256 ~ '{SHA256_CHECK}' AND original_sha256 ~ '{SHA256_CHECK}'",
            name="reference_document_archive_sha256",
        ),
        CheckConstraint(
            "gzip_bytes >= 1 AND original_bytes >= 1",
            name="reference_document_archive_bytes_positive",
        ),
        CheckConstraint(
            "kind IN ('constituent_report', 'change_workbook', 'review_release')",
            name="reference_document_archive_kind",
        ),
        CheckConstraint(
            "object_key <> '' AND object_etag <> ''",
            name="reference_document_archive_object_present",
        ),
        Index(
            "idx_reference_document_archive_date",
            "publisher",
            "kind",
            "series",
            "document_date",
        ),
        Index("idx_reference_document_archive_original", "original_sha256"),
    )

    archive_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    # The Source provider that delivered it, and the organisation that published it.
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    publisher: Mapped[str] = mapped_column(String(50), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    # Index family of a report or workbook (``hsi``, ``hsci``); none for a release.
    series: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Date the content speaks for: the report's trading day, the workbook's
    # latest effective date, or the release's publication date (publisher-local).
    document_date: Mapped[date] = mapped_column(Date, nullable=False)
    gzip_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    gzip_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    source_last_modified: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_etag: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_etag: Mapped[str] = mapped_column(String(100), nullable=False)
    # The delivery that first referenced the document.
    run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReferenceDeliveryEvidence(Base):
    """A document part a reference dataset stored completely.

    A reference work delivers every part it has not seen before; a part already
    stored is proven here instead of delivered again.  Written in the same
    transaction as the part's rows, so a part is never marked stored early.
    """

    __tablename__ = "reference_delivery_evidence"
    __table_args__ = (
        UniqueConstraint(
            "dataset_key",
            "part_key",
            "document_sha256",
            name="uq_reference_delivery_evidence_part",
        ),
        CheckConstraint(
            f"document_sha256 ~ '{SHA256_CHECK}'", name="reference_delivery_evidence_sha256"
        ),
        CheckConstraint("part_key <> ''", name="reference_delivery_evidence_part_key"),
    )

    evidence_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    dataset_key: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT"),
        nullable=False,
    )
    # Which part of the document: an index code, or the whole document.
    part_key: Mapped[str] = mapped_column(String(100), nullable=False)
    # The document as published, so a republished document is a new part.
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    archive_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reference_document_archive.archive_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IndexDefinition(Base):
    """One index a publisher computes, as its documents describe it.

    An industry index carries the classification its membership means.
    ``history_start`` is the earliest change the publisher still lists:
    membership before it is not known and never inferred.
    """

    __tablename__ = "index_definition"
    __table_args__ = (
        UniqueConstraint("publisher", "code", name="uq_index_definition_code"),
        CheckConstraint(f"code ~ '{INDEX_CODE_CHECK}'", name="index_definition_code"),
        CheckConstraint(
            "(classification_scheme IS NULL) = (classification_code IS NULL)",
            name="index_definition_classification_pair",
        ),
        CheckConstraint(
            "parent_index_id IS NULL OR parent_index_id <> index_id",
            name="index_definition_not_own_parent",
        ),
    )

    index_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    publisher: Mapped[str] = mapped_column(String(50), nullable=False)
    code: Mapped[str] = mapped_column(String(8), nullable=False)
    name_en: Mapped[str] = mapped_column(String(200), nullable=False)
    name_zh: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    parent_index_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_definition.index_id", ondelete="RESTRICT"),
        nullable=True,
    )
    classification_scheme: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    classification_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    classification_name_en: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    classification_name_zh: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    history_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class IndexChangeEvent(Base):
    """One effective addition or deletion, as the publisher's workbook lists it.

    Each delivery of an index's workbook replaces what is known of it: an event
    the publisher no longer lists becomes ``retracted`` rather than deleted.
    """

    __tablename__ = "index_change_event"
    __table_args__ = (
        UniqueConstraint(
            "index_id", "effective_date", "symbol", "action", name="uq_index_change_event"
        ),
        CheckConstraint("action IN ('add', 'delete')", name="index_change_event_action"),
        CheckConstraint("status IN ('effective', 'retracted')", name="index_change_event_status"),
        CheckConstraint(f"symbol ~ '{SYMBOL_CHECK}'", name="index_change_event_symbol"),
        CheckConstraint("constituents_after >= 0", name="index_change_event_count"),
        Index("idx_index_change_event_date", "index_id", "effective_date"),
        Index("idx_index_change_event_symbol", "symbol"),
    )

    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    index_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_definition.index_id", ondelete="RESTRICT"),
        nullable=False,
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    symbol: Mapped[str] = mapped_column(String(5), nullable=False)
    name_en: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    name_zh: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    listing_place: Mapped[str] = mapped_column(String(50), nullable=False)
    # Constituent count after every change of that effective date.
    constituents_after: Mapped[int] = mapped_column(Integer, nullable=False)
    # The signed count change the publisher states for that date, when stated.
    count_change: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="effective")
    archive_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reference_document_archive.archive_id", ondelete="RESTRICT"),
        nullable=False,
    )
    first_seen_run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    last_seen_run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class IndexChangeAnnouncement(Base):
    """A change an index review release announced before it took effect.

    ``announced`` until the workbook lists the same change (``confirmed``), a
    later release replaces it (``superseded``), or it is still missing after
    taking effect (``unconfirmed``).
    """

    __tablename__ = "index_change_announcement"
    __table_args__ = (
        UniqueConstraint(
            "archive_id", "index_id", "symbol", "action", name="uq_index_change_announcement"
        ),
        CheckConstraint("action IN ('add', 'delete')", name="index_change_announcement_action"),
        CheckConstraint(
            "status IN ('announced', 'confirmed', 'superseded', 'unconfirmed')",
            name="index_change_announcement_status",
        ),
        CheckConstraint(f"symbol ~ '{SYMBOL_CHECK}'", name="index_change_announcement_symbol"),
        CheckConstraint(
            "(status = 'confirmed') = (confirmed_event_id IS NOT NULL)",
            name="index_change_announcement_confirmation",
        ),
        Index("idx_index_change_announcement_date", "index_id", "effective_date"),
    )

    announcement_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    index_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_definition.index_id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(5), nullable=False)
    name_en: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    listing_place: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="announced")
    confirmed_event_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_change_event.event_id", ondelete="RESTRICT"),
        nullable=True,
    )
    archive_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reference_document_archive.archive_id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class IndexSnapshot(Base):
    """An index's complete constituent list on one trading day, from its report.

    The report of day D includes every change effective on or before D.
    """

    __tablename__ = "index_snapshot"
    __table_args__ = (
        UniqueConstraint("index_id", "trade_date", name="uq_index_snapshot_day"),
        CheckConstraint("constituent_count >= 1", name="index_snapshot_count"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    index_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_definition.index_id", ondelete="RESTRICT"),
        nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    constituent_count: Mapped[int] = mapped_column(Integer, nullable=False)
    archive_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("reference_document_archive.archive_id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IndexSnapshotConstituent(Base):
    """One constituent's line in a snapshot: its weight in that index that day."""

    __tablename__ = "index_snapshot_constituent"
    __table_args__ = (
        CheckConstraint(f"symbol ~ '{SYMBOL_CHECK}'", name="index_snapshot_constituent_symbol"),
        CheckConstraint(
            "weight_pct >= 0 AND weight_pct <= 100", name="index_snapshot_constituent_weight"
        ),
        Index("idx_index_snapshot_constituent_symbol", "symbol"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_snapshot.snapshot_id", ondelete="CASCADE"),
        primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(String(5), primary_key=True)
    name_en: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    name_zh: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    listing_place: Mapped[str] = mapped_column(String(50), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False)
    close: Mapped[Optional[Decimal]] = mapped_column(NUMERIC(20, 6), nullable=True)
    weight_pct: Mapped[Decimal] = mapped_column(NUMERIC(9, 4), nullable=False)
    # The report's own industry column, kept to check it against membership.
    industry_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)


class IndexMembershipBuild(Base):
    """One reconstruction of an index family's membership history.

    ``consistent`` builds replace the family's membership; ``no_anchor`` means
    changes are known but no snapshot exists yet to anchor them.
    """

    __tablename__ = "index_membership_build"
    __table_args__ = (
        CheckConstraint("family IN ('hsi', 'hsci')", name="index_membership_build_family"),
        CheckConstraint(
            "status IN ('consistent', 'no_anchor')", name="index_membership_build_status"
        ),
        CheckConstraint(
            f"inputs_sha256 ~ '{SHA256_CHECK}'", name="index_membership_build_inputs_sha256"
        ),
        CheckConstraint(
            "(status = 'consistent') = (anchor_trade_date IS NOT NULL)",
            name="index_membership_build_anchor",
        ),
        Index("idx_index_membership_build_family", "publisher", "family", "created_at"),
    )

    build_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    publisher: Mapped[str] = mapped_column(String(50), nullable=False)
    family: Mapped[str] = mapped_column(String(20), nullable=False)
    anchor_trade_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    inputs_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    run_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IndexMembership(Base):
    """A symbol's membership of an index over ``[valid_from, valid_to)``.

    Derived by reconstruction, never delivered.  ``valid_to`` is empty while
    the symbol is still a member.  ``starts_at_history_start`` marks a
    membership whose real start is before the publisher's earliest listed
    change; ``valid_from`` is then only the first date it is known.
    """

    __tablename__ = "index_membership"
    __table_args__ = (
        UniqueConstraint("index_id", "symbol", "valid_from", name="uq_index_membership_interval"),
        CheckConstraint(f"symbol ~ '{SYMBOL_CHECK}'", name="index_membership_symbol"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="index_membership_interval"
        ),
        Index("idx_index_membership_symbol", "symbol", "valid_from"),
        Index("idx_index_membership_instrument", "instrument_id"),
    )

    membership_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid7
    )
    index_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_definition.index_id", ondelete="RESTRICT"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(5), nullable=False)
    instrument_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.instrument_id", ondelete="SET NULL"),
        nullable=True,
    )
    name_en: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    starts_at_history_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    from_event_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_change_event.event_id", ondelete="SET NULL"),
        nullable=True,
    )
    to_event_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_change_event.event_id", ondelete="SET NULL"),
        nullable=True,
    )
    build_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_membership_build.build_id", ondelete="RESTRICT"),
        nullable=False,
    )
