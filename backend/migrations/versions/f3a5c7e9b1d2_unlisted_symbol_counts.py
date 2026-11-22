"""Count served symbols written outside the frozen list.

Revision ID: f3a5c7e9b1d2
Revises: d7f9b1c3e5a8

The frozen list measures gaps; a feed that delivers unlisted symbols also writes
every other symbol its provider served. ``daily_delivery_plan_summary`` keeps how
many such symbols a plan wrote, and ``historical_backfill_item`` the symbols one
backfilled date wrote. Both columns are additive: no plan or date wrote unlisted
symbols before this revision, so existing summaries are 0 and existing dates are
null, and a previous release ignores them. Downgrade is refused once any plan or
date has recorded unlisted writes, because dropping the columns would lose them.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "f3a5c7e9b1d2"
down_revision = "d7f9b1c3e5a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_delivery_plan_summary",
        sa.Column("unlisted_data_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "historical_backfill_item",
        sa.Column("unlisted_symbols", pg.JSONB(), nullable=True),
    )


def downgrade() -> None:
    recorded = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM daily_delivery_plan_summary"
            " WHERE unlisted_data_count > 0)"
            " OR EXISTS (SELECT 1 FROM historical_backfill_item"
            " WHERE unlisted_symbols IS NOT NULL)"
        )
    )
    if recorded:
        raise RuntimeError("unlisted writes are recorded; roll forward instead of dropping them")
    op.drop_column("historical_backfill_item", "unlisted_symbols")
    op.drop_column("daily_delivery_plan_summary", "unlisted_data_count")
