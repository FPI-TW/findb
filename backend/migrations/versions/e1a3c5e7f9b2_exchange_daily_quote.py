"""Store the exchange's official daily quotation and archive the reports it came from.

Revision ID: e1a3c5e7f9b2
Revises: c4e6a8b0d2f4

``exchange_report_archive`` indexes exchange reports kept for good in the
canonical bucket; ``exchange_daily_quote`` holds each listed security's line of
such a report. Both tables are additive and no dataset writes them yet, so a
previous release ignores them. Downgrade is refused while the archive has rows,
because dropping them would orphan every archived report.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "e1a3c5e7f9b2"
down_revision = "c4e6a8b0d2f4"
branch_labels = None
depends_on = None


def _run_id() -> sa.Column:
    return sa.Column(
        "run_id",
        pg.UUID(as_uuid=True),
        sa.ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
        nullable=True,
    )


def upgrade() -> None:
    op.create_table(
        "exchange_report_archive",
        sa.Column("archive_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("board", sa.String(10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("gzip_sha256", sa.String(64), nullable=False),
        sa.Column("gzip_bytes", sa.BigInteger(), nullable=False),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("original_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_url", sa.String(200), nullable=False),
        sa.Column("source_last_modified", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_etag", sa.String(100), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("object_etag", sa.String(100), nullable=False),
        _run_id(),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("gzip_sha256", name="uq_exchange_report_archive_gzip"),
        sa.UniqueConstraint("object_key", name="uq_exchange_report_archive_key"),
        sa.CheckConstraint(
            "gzip_sha256 ~ '^[0-9a-f]{64}$' AND original_sha256 ~ '^[0-9a-f]{64}$'",
            name="exchange_report_archive_sha256",
        ),
        sa.CheckConstraint(
            "gzip_bytes >= 1 AND original_bytes >= 1",
            name="exchange_report_archive_bytes_positive",
        ),
        sa.CheckConstraint(
            "object_key <> '' AND object_etag <> ''",
            name="exchange_report_archive_object_present",
        ),
    )
    op.create_index(
        "idx_exchange_report_archive_date",
        "exchange_report_archive",
        ["source", "trade_date", "board"],
    )
    op.create_index(
        "idx_exchange_report_archive_original", "exchange_report_archive", ["original_sha256"]
    )
    op.create_table(
        "exchange_daily_quote",
        sa.Column(
            "instrument_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("instruments.instrument_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("trade_date", sa.Date(), primary_key=True),
        sa.Column("board", sa.String(10), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("trading_status", sa.String(10), nullable=False),
        sa.Column("previous_close", pg.NUMERIC(20, 8), nullable=True),
        sa.Column("close", pg.NUMERIC(20, 8), nullable=True),
        sa.Column("ask", pg.NUMERIC(20, 8), nullable=True),
        sa.Column("bid", pg.NUMERIC(20, 8), nullable=True),
        sa.Column("high", pg.NUMERIC(20, 8), nullable=True),
        sa.Column("low", pg.NUMERIC(20, 8), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("turnover", pg.NUMERIC(24, 4), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "archive_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("exchange_report_archive.archive_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _run_id(),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "trading_status IN ('traded', 'no_trade')",
            name="exchange_daily_quote_trading_status",
        ),
        sa.CheckConstraint("board IN ('main', 'gem')", name="exchange_daily_quote_board"),
        sa.CheckConstraint(
            "volume >= 0 AND turnover >= 0", name="exchange_daily_quote_volume_turnover"
        ),
    )
    op.create_index("idx_exchange_daily_quote_date", "exchange_daily_quote", ["trade_date"])


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT 1 FROM exchange_report_archive LIMIT 1")).first():
        raise RuntimeError(
            "exchange_report_archive indexes archived exchange reports; downgrade is blocked"
        )
    op.drop_index("idx_exchange_daily_quote_date", table_name="exchange_daily_quote")
    op.drop_table("exchange_daily_quote")
    op.drop_index("idx_exchange_report_archive_original", table_name="exchange_report_archive")
    op.drop_index("idx_exchange_report_archive_date", table_name="exchange_report_archive")
    op.drop_table("exchange_report_archive")
