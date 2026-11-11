"""Keep each delivery part's share of the plan summary.

Revision ID: b4d6f8a1c3e5
Revises: e8b3d5f7a9c2

A terminal run or outcome then reclassifies only its own part and applies the
difference to the plan summary. All columns are nullable: existing plans keep
NULL part counts and ``parts_synced_at`` until their next write takes the full
refresh path, so no backfill runs here. A previous release ignores the columns;
its summary writes leave ``parts_synced_at`` behind ``updated_at``, which forces
a full refresh once this release returns. Downgrade drops only derived counts.
"""

import sqlalchemy as sa
from alembic import op

revision = "b4d6f8a1c3e5"
down_revision = "e8b3d5f7a9c2"
branch_labels = None
depends_on = None

PART_COUNTS = ("data_count", "no_data_count", "missing_count", "blocked_count", "mapping_gap_count")


def upgrade() -> None:
    for column in PART_COUNTS:
        op.add_column("daily_delivery_part", sa.Column(column, sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_daily_delivery_part_delivery_part_summary_counts"),
        "daily_delivery_part",
        "num_nulls(data_count, no_data_count, missing_count, blocked_count, "
        "mapping_gap_count) IN (0, 5) "
        "AND (data_count IS NULL OR (data_count >= 0 AND no_data_count >= 0 "
        "AND missing_count >= 0 AND blocked_count >= 0 AND mapping_gap_count >= 0 "
        "AND mapping_gap_count <= blocked_count AND "
        "expected_count = data_count + no_data_count + missing_count + blocked_count))",
    )
    op.add_column(
        "daily_delivery_plan_summary",
        sa.Column("parts_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("daily_delivery_plan_summary", "parts_synced_at")
    op.drop_constraint(
        op.f("ck_daily_delivery_part_delivery_part_summary_counts"),
        "daily_delivery_part",
        type_="check",
    )
    for column in reversed(PART_COUNTS):
        op.drop_column("daily_delivery_part", column)
