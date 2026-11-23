"""Per-key daily quota for intraday Parquet downloads.

Revision ID: b6d8f0a2c4e7
Revises: 44a94f0f8481

``api_key.intraday_download_daily_limit`` is how many object URLs a key may be
given per UTC day; the key also needs the ``intraday_download`` scope.
``intraday_download_usage`` counts what each key was given per day. It is the
one table the Serve role writes, charged atomically before any URL is issued.

Both are additive: an older release ignores the column (it has a server
default) and the table. Downgrade drops them; the counters only bound the
current day, so nothing durable is lost.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b6d8f0a2c4e7"
down_revision = "44a94f0f8481"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_key",
        sa.Column(
            "intraday_download_daily_limit",
            sa.Integer(),
            nullable=False,
            server_default="5000",
        ),
    )
    op.create_check_constraint(
        "api_key_intraday_download_daily_limit_nonnegative",
        "api_key",
        "intraday_download_daily_limit >= 0",
    )
    op.create_table(
        "intraday_download_usage",
        sa.Column("key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("object_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["key_id"], ["api_key.key_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("key_id", "usage_date"),
        sa.CheckConstraint(
            "object_count >= 0", name="intraday_download_usage_object_count_nonnegative"
        ),
    )


def downgrade() -> None:
    op.drop_table("intraday_download_usage")
    op.drop_constraint(
        "api_key_intraday_download_daily_limit_nonnegative", "api_key", type_="check"
    )
    op.drop_column("api_key", "intraday_download_daily_limit")
