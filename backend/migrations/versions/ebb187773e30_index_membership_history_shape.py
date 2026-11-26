"""Index membership history: unknown starts, observed range, families; no part evidence.

Revision ID: ebb187773e30
Revises: 37ed18886cab

A membership that began before the publisher's earliest recorded change has no
``valid_from``, replacing ``starts_at_history_start``.  A build records the
first snapshot day (``observed_from``), before which membership is derived from
the published changes, and how many disagreements between the sources it found.
``index_definition.series`` names the family an index belongs to, and the
interval key treats an unknown start as one value (``NULLS NOT DISTINCT``).
``reference_delivery_evidence`` is dropped: reference documents are delivered
as members of daily plans, which already record what each day received.

The tables are still empty in every environment, since no dataset writes them,
so the columns are added without defaults. Downgrade is refused once any index
is defined, because the earlier shape cannot hold unknown starts.
"""

import sqlalchemy as sa
from alembic import op

revision = "ebb187773e30"
down_revision = "37ed18886cab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("reference_delivery_evidence")
    op.add_column("index_definition", sa.Column("series", sa.String(length=20), nullable=False))
    op.create_index(
        "idx_index_definition_series", "index_definition", ["publisher", "series"], unique=False
    )
    op.alter_column("index_membership", "valid_from", existing_type=sa.Date(), nullable=True)
    op.drop_column("index_membership", "starts_at_history_start")
    op.drop_constraint(
        op.f("ck_index_membership_index_membership_interval"), "index_membership", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_index_membership_index_membership_interval"),
        "index_membership",
        "valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from",
    )
    # An unknown start is one value: a code has one membership before history.
    op.drop_constraint("uq_index_membership_interval", "index_membership", type_="unique")
    op.create_unique_constraint(
        "uq_index_membership_interval",
        "index_membership",
        ["index_id", "symbol", "valid_from"],
        postgresql_nulls_not_distinct=True,
    )
    op.add_column("index_membership_build", sa.Column("observed_from", sa.Date(), nullable=True))
    op.add_column(
        "index_membership_build",
        sa.Column("findings", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("index_membership_build", "findings", server_default=None)
    op.create_check_constraint(
        op.f("ck_index_membership_build_index_membership_build_observed"),
        "index_membership_build",
        "(anchor_trade_date IS NULL) = (observed_from IS NULL) "
        "AND (observed_from IS NULL OR observed_from <= anchor_trade_date)",
    )
    op.create_check_constraint(
        op.f("ck_index_membership_build_index_membership_build_findings"),
        "index_membership_build",
        "findings >= 0",
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM index_definition LIMIT 1")).first():
        raise RuntimeError("index_definition holds indexes; downgrade is blocked")
    op.drop_constraint(
        op.f("ck_index_membership_build_index_membership_build_findings"),
        "index_membership_build",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_index_membership_build_index_membership_build_observed"),
        "index_membership_build",
        type_="check",
    )
    op.drop_column("index_membership_build", "findings")
    op.drop_column("index_membership_build", "observed_from")
    op.drop_constraint("uq_index_membership_interval", "index_membership", type_="unique")
    op.create_unique_constraint(
        "uq_index_membership_interval", "index_membership", ["index_id", "symbol", "valid_from"]
    )
    op.drop_constraint(
        op.f("ck_index_membership_index_membership_interval"), "index_membership", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_index_membership_index_membership_interval"),
        "index_membership",
        "valid_to IS NULL OR valid_to > valid_from",
    )
    op.add_column(
        "index_membership",
        sa.Column("starts_at_history_start", sa.Boolean(), nullable=False),
    )
    op.alter_column("index_membership", "valid_from", existing_type=sa.Date(), nullable=False)
    op.drop_index("idx_index_definition_series", table_name="index_definition")
    op.drop_column("index_definition", "series")
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
            "dataset_key",
            "part_key",
            "document_sha256",
            name="uq_reference_delivery_evidence_part",
        ),
    )
