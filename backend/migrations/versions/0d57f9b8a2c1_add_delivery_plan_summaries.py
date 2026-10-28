"""Persist write-maintained delivery plan summaries.

Revision ID: 0d57f9b8a2c1
Revises: f97d2a6c4b81
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0d57f9b8a2c1"
down_revision = "f97d2a6c4b81"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_delivery_plan_summary",
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("data_count", sa.Integer(), nullable=False),
        sa.Column("no_data_count", sa.Integer(), nullable=False),
        sa.Column("missing_count", sa.Integer(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False),
        sa.Column("latest_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("latest_run_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_code", sa.String(50), nullable=True),
        sa.Column("policy_outcome", sa.String(20), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "expected_count >= 0 AND data_count >= 0 AND no_data_count >= 0 "
            "AND missing_count >= 0 AND blocked_count >= 0 AND "
            "expected_count = data_count + no_data_count + missing_count + blocked_count",
            name=op.f("ck_daily_delivery_plan_summary_delivery_plan_summary_counts"),
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["daily_delivery_plan.plan_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("plan_id"),
    )


def downgrade() -> None:
    op.drop_table("daily_delivery_plan_summary")
