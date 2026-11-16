"""Index the canonical intraday objects by dataset, symbol and trade date.

Revision ID: d6f8b0c2e4a7
Revises: c5e7a9b1d3f6

Intraday bars and ticks are stored as Parquet objects in the canonical bucket.
This table is the only record of which object currently holds a symbol's
trading day and at which version, so completeness and Serve never list the
bucket. The table is additive: a previous release ignores it. Downgrade is
refused while it has rows, because dropping them would orphan every object.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "d6f8b0c2e4a7"
down_revision = "c5e7a9b1d3f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "intraday_partition",
        sa.Column(
            "dataset_key",
            sa.String(50),
            sa.ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("symbol", sa.String(50), primary_key=True),
        sa.Column("trade_date", sa.Date(), primary_key=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_raw_sha256", sa.String(64), nullable=False),
        sa.Column("source_rows_sha256", sa.String(64), nullable=False),
        sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("object_layout", sa.String(10), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("object_etag", sa.String(100), nullable=False),
        sa.Column(
            "run_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "symbol ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$'",
            name="intraday_partition_symbol_key_safe",
        ),
        sa.CheckConstraint("row_count >= 1", name="intraday_partition_row_count_positive"),
        sa.CheckConstraint(
            "source_raw_sha256 ~ '^[0-9a-f]{64}$' AND source_rows_sha256 ~ '^[0-9a-f]{64}$'",
            name="intraday_partition_source_sha256",
        ),
        sa.CheckConstraint(
            "object_layout IN ('day', 'month')",
            name="intraday_partition_object_layout",
        ),
        sa.CheckConstraint(
            "object_key <> '' AND object_etag <> ''",
            name="intraday_partition_object_present",
        ),
    )
    op.create_index(
        "idx_intraday_partition_dataset_date",
        "intraday_partition",
        ["dataset_key", "trade_date"],
    )


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM intraday_partition LIMIT 1")).first():
        raise RuntimeError(
            "intraday_partition indexes stored canonical objects; downgrade is blocked"
        )
    op.drop_index("idx_intraday_partition_dataset_date", table_name="intraday_partition")
    op.drop_table("intraday_partition")
