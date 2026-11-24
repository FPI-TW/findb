"""Storage for index reference data: documents, changes, snapshots, membership.

Revision ID: 2bbe07b98293
Revises: b6d8f0a2c4e7

An index publisher's constituent reports, change workbooks and review releases
are kept in ``reference_document_archive``; what they state is kept in
``index_definition``, ``index_change_event``, ``index_change_announcement``
and ``index_snapshot``/``index_snapshot_constituent``.  ``index_membership``
and ``index_membership_build`` hold the membership history reconstructed from
them, and ``reference_delivery_evidence`` records which document parts a
reference dataset has stored.

All tables are new and no dataset writes them yet, so a previous release
ignores them. Downgrade is refused once any document is archived, because
dropping the tables would lose documents the publisher no longer serves.
"""

import sqlalchemy as sa
from alembic import op

revision = "2bbe07b98293"
down_revision = "b6d8f0a2c4e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reference_document_archive",
        sa.Column("archive_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("publisher", sa.String(length=50), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("series", sa.String(length=20), nullable=True),
        sa.Column("document_date", sa.Date(), nullable=False),
        sa.Column("gzip_sha256", sa.String(length=64), nullable=False),
        sa.Column("gzip_bytes", sa.BigInteger(), nullable=False),
        sa.Column("original_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("source_last_modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_etag", sa.String(length=100), nullable=True),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("object_etag", sa.String(length=100), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "gzip_sha256 ~ '^[0-9a-f]{64}$' AND original_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_reference_document_archive_reference_document_archive_sha256"),
        ),
        sa.CheckConstraint(
            "kind IN ('constituent_report', 'change_workbook', 'review_release')",
            name=op.f("ck_reference_document_archive_reference_document_archive_kind"),
        ),
        sa.CheckConstraint(
            "object_key <> '' AND object_etag <> ''",
            name=op.f("ck_reference_document_archive_reference_document_archive_object_present"),
        ),
        sa.CheckConstraint(
            "gzip_bytes >= 1 AND original_bytes >= 1",
            name=op.f("ck_reference_document_archive_reference_document_archive_bytes_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_reference_document_archive_run_id_ingestion_run"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("archive_id", name=op.f("pk_reference_document_archive")),
        sa.UniqueConstraint("gzip_sha256", name="uq_reference_document_archive_gzip"),
        sa.UniqueConstraint("object_key", name="uq_reference_document_archive_key"),
    )
    op.create_index(
        "idx_reference_document_archive_date",
        "reference_document_archive",
        ["publisher", "kind", "series", "document_date"],
        unique=False,
    )
    op.create_index(
        "idx_reference_document_archive_original",
        "reference_document_archive",
        ["original_sha256"],
        unique=False,
    )
    op.create_table(
        "index_definition",
        sa.Column("index_id", sa.UUID(), nullable=False),
        sa.Column("publisher", sa.String(length=50), nullable=False),
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=False),
        sa.Column("name_zh", sa.String(length=200), nullable=True),
        sa.Column("parent_index_id", sa.UUID(), nullable=True),
        sa.Column("classification_scheme", sa.String(length=20), nullable=True),
        sa.Column("classification_code", sa.String(length=10), nullable=True),
        sa.Column("classification_name_en", sa.String(length=100), nullable=True),
        sa.Column("classification_name_zh", sa.String(length=100), nullable=True),
        sa.Column("history_start", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "code ~ '^[0-9]{5}\\.[0-9]{2}$'", name=op.f("ck_index_definition_index_definition_code")
        ),
        sa.CheckConstraint(
            "(classification_scheme IS NULL) = (classification_code IS NULL)",
            name=op.f("ck_index_definition_index_definition_classification_pair"),
        ),
        sa.CheckConstraint(
            "parent_index_id IS NULL OR parent_index_id <> index_id",
            name=op.f("ck_index_definition_index_definition_not_own_parent"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_index_id"],
            ["index_definition.index_id"],
            name=op.f("fk_index_definition_parent_index_id_index_definition"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("index_id", name=op.f("pk_index_definition")),
        sa.UniqueConstraint("publisher", "code", name="uq_index_definition_code"),
    )
    op.create_table(
        "index_change_event",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("index_id", sa.UUID(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("symbol", sa.String(length=5), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("name_zh", sa.String(length=200), nullable=True),
        sa.Column("listing_place", sa.String(length=50), nullable=False),
        sa.Column("constituents_after", sa.Integer(), nullable=False),
        sa.Column("count_change", sa.Integer(), nullable=True),
        sa.Column("source_ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("archive_id", sa.UUID(), nullable=False),
        sa.Column("first_seen_run_id", sa.UUID(), nullable=True),
        sa.Column("last_seen_run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('add', 'delete')",
            name=op.f("ck_index_change_event_index_change_event_action"),
        ),
        sa.CheckConstraint(
            "status IN ('effective', 'retracted')",
            name=op.f("ck_index_change_event_index_change_event_status"),
        ),
        sa.CheckConstraint(
            "symbol ~ '^[0-9]{5}$'", name=op.f("ck_index_change_event_index_change_event_symbol")
        ),
        sa.CheckConstraint(
            "constituents_after >= 0", name=op.f("ck_index_change_event_index_change_event_count")
        ),
        sa.ForeignKeyConstraint(
            ["archive_id"],
            ["reference_document_archive.archive_id"],
            name=op.f("fk_index_change_event_archive_id_reference_document_archive"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_index_change_event_first_seen_run_id_ingestion_run"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["index_id"],
            ["index_definition.index_id"],
            name=op.f("fk_index_change_event_index_id_index_definition"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_seen_run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_index_change_event_last_seen_run_id_ingestion_run"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_index_change_event")),
        sa.UniqueConstraint(
            "index_id", "effective_date", "symbol", "action", name="uq_index_change_event"
        ),
    )
    op.create_index(
        "idx_index_change_event_date",
        "index_change_event",
        ["index_id", "effective_date"],
        unique=False,
    )
    op.create_index("idx_index_change_event_symbol", "index_change_event", ["symbol"], unique=False)
    op.create_table(
        "index_change_announcement",
        sa.Column("announcement_id", sa.UUID(), nullable=False),
        sa.Column("index_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=5), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("listing_place", sa.String(length=50), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("confirmed_event_id", sa.UUID(), nullable=True),
        sa.Column("archive_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'confirmed') = (confirmed_event_id IS NOT NULL)",
            name=op.f("ck_index_change_announcement_index_change_announcement_confirmation"),
        ),
        sa.CheckConstraint(
            "action IN ('add', 'delete')",
            name=op.f("ck_index_change_announcement_index_change_announcement_action"),
        ),
        sa.CheckConstraint(
            "status IN ('announced', 'confirmed', 'superseded', 'unconfirmed')",
            name=op.f("ck_index_change_announcement_index_change_announcement_status"),
        ),
        sa.CheckConstraint(
            "symbol ~ '^[0-9]{5}$'",
            name=op.f("ck_index_change_announcement_index_change_announcement_symbol"),
        ),
        sa.ForeignKeyConstraint(
            ["archive_id"],
            ["reference_document_archive.archive_id"],
            name=op.f("fk_index_change_announcement_archive_id_reference_document_archive"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_event_id"],
            ["index_change_event.event_id"],
            name=op.f("fk_index_change_announcement_confirmed_event_id_index_change_event"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["index_id"],
            ["index_definition.index_id"],
            name=op.f("fk_index_change_announcement_index_id_index_definition"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_index_change_announcement_run_id_ingestion_run"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("announcement_id", name=op.f("pk_index_change_announcement")),
        sa.UniqueConstraint(
            "archive_id", "index_id", "symbol", "action", name="uq_index_change_announcement"
        ),
    )
    op.create_index(
        "idx_index_change_announcement_date",
        "index_change_announcement",
        ["index_id", "effective_date"],
        unique=False,
    )
    op.create_table(
        "index_snapshot",
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("index_id", sa.UUID(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("constituent_count", sa.Integer(), nullable=False),
        sa.Column("archive_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "constituent_count >= 1", name=op.f("ck_index_snapshot_index_snapshot_count")
        ),
        sa.ForeignKeyConstraint(
            ["archive_id"],
            ["reference_document_archive.archive_id"],
            name=op.f("fk_index_snapshot_archive_id_reference_document_archive"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["index_id"],
            ["index_definition.index_id"],
            name=op.f("fk_index_snapshot_index_id_index_definition"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_index_snapshot_run_id_ingestion_run"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_index_snapshot")),
        sa.UniqueConstraint("index_id", "trade_date", name="uq_index_snapshot_day"),
    )
    op.create_table(
        "index_snapshot_constituent",
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=5), nullable=False),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("name_zh", sa.String(length=200), nullable=True),
        sa.Column("listing_place", sa.String(length=50), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False),
        sa.Column("close", sa.NUMERIC(precision=20, scale=6), nullable=True),
        sa.Column("weight_pct", sa.NUMERIC(precision=9, scale=4), nullable=False),
        sa.Column("industry_code", sa.String(length=10), nullable=True),
        sa.CheckConstraint(
            "symbol ~ '^[0-9]{5}$'",
            name=op.f("ck_index_snapshot_constituent_index_snapshot_constituent_symbol"),
        ),
        sa.CheckConstraint(
            "weight_pct >= 0 AND weight_pct <= 100",
            name=op.f("ck_index_snapshot_constituent_index_snapshot_constituent_weight"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["index_snapshot.snapshot_id"],
            name=op.f("fk_index_snapshot_constituent_snapshot_id_index_snapshot"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_id", "symbol", name=op.f("pk_index_snapshot_constituent")
        ),
    )
    op.create_index(
        "idx_index_snapshot_constituent_symbol",
        "index_snapshot_constituent",
        ["symbol"],
        unique=False,
    )
    op.create_table(
        "index_membership_build",
        sa.Column("build_id", sa.UUID(), nullable=False),
        sa.Column("publisher", sa.String(length=50), nullable=False),
        sa.Column("family", sa.String(length=20), nullable=False),
        sa.Column("anchor_trade_date", sa.Date(), nullable=True),
        sa.Column("inputs_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'consistent') = (anchor_trade_date IS NOT NULL)",
            name=op.f("ck_index_membership_build_index_membership_build_anchor"),
        ),
        sa.CheckConstraint(
            "family IN ('hsi', 'hsci')",
            name=op.f("ck_index_membership_build_index_membership_build_family"),
        ),
        sa.CheckConstraint(
            "inputs_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_index_membership_build_index_membership_build_inputs_sha256"),
        ),
        sa.CheckConstraint(
            "status IN ('consistent', 'no_anchor')",
            name=op.f("ck_index_membership_build_index_membership_build_status"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_index_membership_build_run_id_ingestion_run"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("build_id", name=op.f("pk_index_membership_build")),
    )
    op.create_index(
        "idx_index_membership_build_family",
        "index_membership_build",
        ["publisher", "family", "created_at"],
        unique=False,
    )
    op.create_table(
        "index_membership",
        sa.Column("membership_id", sa.UUID(), nullable=False),
        sa.Column("index_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=5), nullable=False),
        sa.Column("instrument_id", sa.UUID(), nullable=True),
        sa.Column("name_en", sa.String(length=200), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("starts_at_history_start", sa.Boolean(), nullable=False),
        sa.Column("from_event_id", sa.UUID(), nullable=True),
        sa.Column("to_event_id", sa.UUID(), nullable=True),
        sa.Column("build_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "symbol ~ '^[0-9]{5}$'", name=op.f("ck_index_membership_index_membership_symbol")
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name=op.f("ck_index_membership_index_membership_interval"),
        ),
        sa.ForeignKeyConstraint(
            ["build_id"],
            ["index_membership_build.build_id"],
            name=op.f("fk_index_membership_build_id_index_membership_build"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["from_event_id"],
            ["index_change_event.event_id"],
            name=op.f("fk_index_membership_from_event_id_index_change_event"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["index_id"],
            ["index_definition.index_id"],
            name=op.f("fk_index_membership_index_id_index_definition"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_index_membership_instrument_id_instruments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["to_event_id"],
            ["index_change_event.event_id"],
            name=op.f("fk_index_membership_to_event_id_index_change_event"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("membership_id", name=op.f("pk_index_membership")),
        sa.UniqueConstraint(
            "index_id", "symbol", "valid_from", name="uq_index_membership_interval"
        ),
    )
    op.create_index(
        "idx_index_membership_instrument", "index_membership", ["instrument_id"], unique=False
    )
    op.create_index(
        "idx_index_membership_symbol", "index_membership", ["symbol", "valid_from"], unique=False
    )
    op.create_table(
        "reference_delivery_evidence",
        sa.Column("evidence_id", sa.UUID(), nullable=False),
        sa.Column("dataset_key", sa.String(length=50), nullable=False),
        sa.Column("part_key", sa.String(length=100), nullable=False),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("archive_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "document_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_reference_delivery_evidence_reference_delivery_evidence_sha256"),
        ),
        sa.CheckConstraint(
            "part_key <> ''",
            name=op.f("ck_reference_delivery_evidence_reference_delivery_evidence_part_key"),
        ),
        sa.ForeignKeyConstraint(
            ["archive_id"],
            ["reference_document_archive.archive_id"],
            name=op.f("fk_reference_delivery_evidence_archive_id_reference_document_archive"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_key"],
            ["dataset_registry.dataset_key"],
            name=op.f("fk_reference_delivery_evidence_dataset_key_dataset_registry"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_reference_delivery_evidence_run_id_ingestion_run"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("evidence_id", name=op.f("pk_reference_delivery_evidence")),
        sa.UniqueConstraint(
            "dataset_key", "part_key", "document_sha256", name="uq_reference_delivery_evidence_part"
        ),
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM reference_document_archive LIMIT 1")).first():
        raise RuntimeError(
            "reference_document_archive keeps publisher documents; downgrade is blocked"
        )
    op.drop_table("reference_delivery_evidence")
    op.drop_index("idx_index_membership_instrument", table_name="index_membership")
    op.drop_index("idx_index_membership_symbol", table_name="index_membership")
    op.drop_table("index_membership")
    op.drop_index("idx_index_membership_build_family", table_name="index_membership_build")
    op.drop_table("index_membership_build")
    op.drop_index("idx_index_snapshot_constituent_symbol", table_name="index_snapshot_constituent")
    op.drop_table("index_snapshot_constituent")
    op.drop_table("index_snapshot")
    op.drop_index("idx_index_change_announcement_date", table_name="index_change_announcement")
    op.drop_table("index_change_announcement")
    op.drop_index("idx_index_change_event_date", table_name="index_change_event")
    op.drop_index("idx_index_change_event_symbol", table_name="index_change_event")
    op.drop_table("index_change_event")
    op.drop_table("index_definition")
    op.drop_index("idx_reference_document_archive_date", table_name="reference_document_archive")
    op.drop_index(
        "idx_reference_document_archive_original", table_name="reference_document_archive"
    )
    op.drop_table("reference_document_archive")
