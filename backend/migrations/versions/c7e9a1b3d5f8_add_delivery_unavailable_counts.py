"""Count members a provider cannot serve apart from blocked ones (#593).

Revision ID: c7e9a1b3d5f8
Revises: 64da893e5e78

A frozen member without a provider mapping used to count as blocked, so a plan
whose only remaining members were mapping gaps stayed incomplete forever. Those
members now count as ``unavailable``: ``expected = data + no_data + missing +
blocked + unavailable``, and a plan is complete when ``data + no_data +
unavailable = expected``.

Part counts that a full refresh initialized move their mapping gaps from
``blocked_count`` to ``unavailable_count``. A plan summary moves the sum of its
parts' mapping gaps only when every part count is set and the summary was written
together with them (``parts_synced_at = updated_at``); any other summary keeps
its counts, which still satisfy the constraint, and loses ``parts_synced_at`` so
its next write takes the full refresh path. The rotating summary audit corrects
any summary that is read before then.

This revision is a rollback floor: a previous release writes blocked counts that
include mapping gaps without ``unavailable_count`` and would violate the new
constraint. Downgrade folds the counts back into ``blocked_count``.
"""

import sqlalchemy as sa
from alembic import op

revision = "c7e9a1b3d5f8"
down_revision = "64da893e5e78"
branch_labels = None
depends_on = None

SUMMARY_CHECK = "ck_daily_delivery_plan_summary_delivery_plan_summary_counts"
PART_CHECK = "ck_daily_delivery_part_delivery_part_summary_counts"


def upgrade() -> None:
    op.add_column(
        "daily_delivery_plan_summary",
        sa.Column("unavailable_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "daily_delivery_part", sa.Column("unavailable_count", sa.Integer(), nullable=True)
    )
    op.drop_constraint(op.f(SUMMARY_CHECK), "daily_delivery_plan_summary", type_="check")
    op.drop_constraint(op.f(PART_CHECK), "daily_delivery_part", type_="check")
    op.execute(
        """
        UPDATE daily_delivery_plan_summary s
        SET unavailable_count = p.gaps, blocked_count = s.blocked_count - p.gaps
        FROM (
            SELECT plan_id, SUM(mapping_gap_count) AS gaps
            FROM daily_delivery_part
            GROUP BY plan_id
            HAVING COUNT(*) = COUNT(data_count)
        ) p
        WHERE s.plan_id = p.plan_id
          AND p.gaps > 0
          AND s.parts_synced_at = s.updated_at
          AND s.blocked_count >= p.gaps
        """
    )
    # Unconverted summaries still hold mapping gaps as blocked: refresh them fully.
    op.execute(
        """
        UPDATE daily_delivery_plan_summary s
        SET parts_synced_at = NULL
        WHERE s.unavailable_count = 0
          AND EXISTS (
            SELECT 1 FROM daily_delivery_member m
            WHERE m.plan_id = s.plan_id AND m.mapping_status = 'gap'
          )
        """
    )
    op.execute(
        """
        UPDATE daily_delivery_part
        SET unavailable_count = mapping_gap_count,
            blocked_count = blocked_count - mapping_gap_count
        WHERE data_count IS NOT NULL
        """
    )
    op.create_check_constraint(
        op.f("ck_daily_delivery_plan_summary_delivery_plan_summary_counts"),
        "daily_delivery_plan_summary",
        "expected_count >= 0 AND data_count >= 0 AND no_data_count >= 0 "
        "AND missing_count >= 0 AND blocked_count >= 0 AND unavailable_count >= 0 AND "
        "expected_count = data_count + no_data_count + missing_count + blocked_count "
        "+ unavailable_count",
    )
    op.create_check_constraint(
        op.f("ck_daily_delivery_part_delivery_part_summary_counts"),
        "daily_delivery_part",
        "num_nulls(data_count, no_data_count, missing_count, blocked_count, "
        "unavailable_count, mapping_gap_count) IN (0, 6) "
        "AND (data_count IS NULL OR (data_count >= 0 AND no_data_count >= 0 "
        "AND missing_count >= 0 AND blocked_count >= 0 AND unavailable_count >= 0 "
        "AND mapping_gap_count >= 0 AND mapping_gap_count <= unavailable_count AND "
        "expected_count = data_count + no_data_count + missing_count + blocked_count "
        "+ unavailable_count))",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(SUMMARY_CHECK), "daily_delivery_plan_summary", type_="check")
    op.drop_constraint(op.f(PART_CHECK), "daily_delivery_part", type_="check")
    op.execute(
        "UPDATE daily_delivery_plan_summary "
        "SET blocked_count = blocked_count + unavailable_count, unavailable_count = 0"
    )
    op.execute(
        "UPDATE daily_delivery_part SET blocked_count = blocked_count + unavailable_count "
        "WHERE data_count IS NOT NULL"
    )
    op.create_check_constraint(
        op.f("ck_daily_delivery_plan_summary_delivery_plan_summary_counts"),
        "daily_delivery_plan_summary",
        "expected_count >= 0 AND data_count >= 0 AND no_data_count >= 0 "
        "AND missing_count >= 0 AND blocked_count >= 0 AND "
        "expected_count = data_count + no_data_count + missing_count + blocked_count",
    )
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
    op.drop_column("daily_delivery_part", "unavailable_count")
    op.drop_column("daily_delivery_plan_summary", "unavailable_count")
