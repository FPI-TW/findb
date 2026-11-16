"""Index the intraday days that still wait for their month merge.

Revision ID: e7a9c1d3f5b8
Revises: d6f8b0c2e4a7

The Worker looks for symbol-months of finished months that still have a day
object. Without this partial index that lookup reads the whole index table,
which grows by every stored trading day; with it, only unmerged days are read.
The index is additive and downgrade only drops it.
"""

import sqlalchemy as sa
from alembic import op

revision = "e7a9c1d3f5b8"
down_revision = "d6f8b0c2e4a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_intraday_partition_day_layout",
        "intraday_partition",
        ["dataset_key", "symbol", "trade_date"],
        postgresql_where=sa.text("object_layout = 'day'"),
    )


def downgrade() -> None:
    op.drop_index("idx_intraday_partition_day_layout", table_name="intraday_partition")
