"""market_calendar_management

Revision ID: f8a9b0c1d2e3
Revises: 8ad9e0f1a2b3
"""

from datetime import datetime, time, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "8ad9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    calendar_market = op.create_table(
        "calendar_market",
        sa.Column("market", sa.String(10), primary_key=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("weekend_days", postgresql.JSONB(), nullable=False),
        sa.Column("default_session_open", sa.Time(), nullable=True),
        sa.Column("default_session_close", sa.Time(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    seeded_at = datetime.now(timezone.utc)
    op.bulk_insert(
        calendar_market,
        [
            {
                "market": market,
                "display_name": display_name,
                "timezone": market_timezone,
                "weekend_days": weekend_days,
                "default_session_open": session_open,
                "default_session_close": session_close,
                "active": True,
                "created_at": seeded_at,
                "updated_at": seeded_at,
            }
            for (
                market,
                display_name,
                market_timezone,
                weekend_days,
                session_open,
                session_close,
            ) in (
                ("CN", "中國", "Asia/Shanghai", [5, 6], None, None),
                ("CRYPTO", "加密資產", "UTC", [], None, None),
                ("DE", "德國", "Europe/Berlin", [5, 6], None, None),
                ("FX", "外匯", "UTC", [5, 6], None, None),
                ("GLOBAL", "全球", "UTC", [5, 6], None, None),
                ("HK", "香港", "Asia/Hong_Kong", [5, 6], None, None),
                ("IN", "印度", "Asia/Kolkata", [5, 6], None, None),
                ("JP", "日本", "Asia/Tokyo", [5, 6], None, None),
                ("MACRO", "總體經濟", "UTC", [5, 6], None, None),
                ("SE", "瑞典", "Europe/Stockholm", [5, 6], None, None),
                ("TW", "臺灣", "Asia/Taipei", [5, 6], time(9, 0), time(13, 30)),
                ("US", "美國", "America/New_York", [5, 6], None, None),
                ("WTX", "臺灣期貨", "Asia/Taipei", [5, 6], None, None),
            )
        ],
    )
    op.create_table(
        "calendar_year_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expected_days", sa.Integer(), nullable=False),
        sa.Column("actual_days", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=True),
        sa.Column("source_filename", sa.String(255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("market", "year", "revision", name="uq_calendar_year_revision"),
    )
    op.create_index(
        "idx_calendar_year_revision_lookup", "calendar_year_revision", ["market", "year", "status"]
    )
    op.create_index(
        "uq_calendar_year_published",
        "calendar_year_revision",
        ["market", "year"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_table(
        "calendar_revision_day",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "calendar_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calendar_year_revision.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("day_status", sa.String(20), nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column("session_open", sa.Time(), nullable=True),
        sa.Column("session_close", sa.Time(), nullable=True),
        sa.Column("holiday_name", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.String(30), nullable=False),
        sa.UniqueConstraint("calendar_revision_id", "trade_date", name="uq_calendar_revision_day"),
        sa.CheckConstraint(
            "day_status IN ('open', 'closed', 'settlement_only')",
            name="calendar_revision_day_status_valid",
        ),
    )
    op.create_index(
        "idx_calendar_revision_day_date",
        "calendar_revision_day",
        ["calendar_revision_id", "trade_date"],
    )
    op.create_table(
        "calendar_import_batch",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("input_format", sa.String(30), nullable=False),
        sa.Column("detected_encoding", sa.String(30), nullable=True),
        sa.Column("source_filename", sa.String(255), nullable=True),
        sa.Column("source_sha256", sa.String(64), nullable=True),
        sa.Column("candidate_rows", postgresql.JSONB(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("warnings", postgresql.JSONB(), nullable=False),
        sa.Column("errors", postgresql.JSONB(), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_calendar_import_batch_lookup", "calendar_import_batch", ["market", "year", "status"]
    )
    op.add_column(
        "trading_calendar",
        sa.Column("day_status", sa.String(20), nullable=False, server_default="open"),
    )
    op.add_column("trading_calendar", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "trading_calendar",
        sa.Column(
            "source_kind", sa.String(30), nullable=False, server_default="observed_ingestion"
        ),
    )
    op.add_column("trading_calendar", sa.Column("source_reference", sa.String(255), nullable=True))
    op.add_column(
        "trading_calendar",
        sa.Column("import_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "trading_calendar", sa.Column("revision", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "trading_calendar",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('utc', now())"),
        ),
    )
    op.add_column(
        "trading_calendar",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('utc', now())"),
        ),
    )
    op.execute(
        """
        UPDATE trading_calendar
        SET day_status = CASE WHEN is_open THEN 'open' ELSE 'closed' END,
            source_kind = 'observed_ingestion',
            revision = 0
        """
    )
    op.create_check_constraint(
        "calendar_day_status_valid",
        "trading_calendar",
        "day_status IN ('open', 'closed', 'settlement_only')",
    )
    op.create_check_constraint(
        "calendar_is_open_consistent", "trading_calendar", "is_open = (day_status = 'open')"
    )


def downgrade() -> None:
    op.drop_constraint("calendar_is_open_consistent", "trading_calendar", type_="check")
    op.drop_constraint("calendar_day_status_valid", "trading_calendar", type_="check")
    for column in (
        "updated_at",
        "created_at",
        "revision",
        "import_batch_id",
        "source_reference",
        "source_kind",
        "description",
        "day_status",
    ):
        op.drop_column("trading_calendar", column)
    op.drop_index("idx_calendar_revision_day_date", table_name="calendar_revision_day")
    op.drop_table("calendar_revision_day")
    op.drop_index("idx_calendar_import_batch_lookup", table_name="calendar_import_batch")
    op.drop_table("calendar_import_batch")
    op.drop_index("uq_calendar_year_published", table_name="calendar_year_revision")
    op.drop_index("idx_calendar_year_revision_lookup", table_name="calendar_year_revision")
    op.drop_table("calendar_year_revision")
    op.drop_table("calendar_market")
