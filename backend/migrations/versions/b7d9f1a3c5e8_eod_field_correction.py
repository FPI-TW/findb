"""Record which EOD fields an exchange quote corrected, and what they held before.

Revision ID: b7d9f1a3c5e8
Revises: a4c6e8f0b2d3

``market_data_eod.field_sources`` names the fields whose value is not the row
source's own; ``eod_field_correction`` keeps, append-only, every value an
exchange quote replaced. Both are additive and no dataset writes them yet: a
previous release ignores the column, though it leaves the column unchanged when
it rewrites a row. Downgrade is refused once any correction is recorded, because
dropping it would lose which canonical values came from the exchange.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "b7d9f1a3c5e8"
down_revision = "a4c6e8f0b2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable without a default: a catalog-only change on every partition.
    op.add_column("market_data_eod", sa.Column("field_sources", pg.JSONB(), nullable=True))
    op.create_table(
        "eod_field_correction",
        sa.Column("correction_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instrument_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("instruments.instrument_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("replaced_source", sa.String(50), nullable=True),
        sa.Column("before", pg.JSONB(), nullable=False),
        sa.Column("after", pg.JSONB(), nullable=False),
        sa.Column(
            "archive_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("exchange_report_archive.archive_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_eod_field_correction_slot", "eod_field_correction", ["instrument_id", "trade_date"]
    )
    op.create_index("idx_eod_field_correction_run", "eod_field_correction", ["run_id"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM eod_field_correction LIMIT 1")).first():
        raise RuntimeError(
            "eod_field_correction records exchange corrections; downgrade is blocked"
        )
    op.drop_index("idx_eod_field_correction_run", table_name="eod_field_correction")
    op.drop_index("idx_eod_field_correction_slot", table_name="eod_field_correction")
    op.drop_table("eod_field_correction")
    op.drop_column("market_data_eod", "field_sources")
