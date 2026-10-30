"""Add append-only Shioaji frozen member repair approvals.

Revision ID: 2f79b1d0c4e3
Revises: 1e68a0c9b3d2
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "2f79b1d0c4e3"
down_revision = "1e68a0c9b3d2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shioaji_member_repair",
        sa.Column("repair_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "work_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("provider_work.work_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("daily_delivery_plan.plan_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("execution_profile", sa.String(20), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("member_key", sa.String(150), nullable=False),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("provider_symbol", sa.String(100), nullable=True),
        sa.Column("observation_limit", sa.Integer(), nullable=False),
        sa.Column("evidence", pg.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "work_id", "request_id", "member_key", name="uq_shioaji_repair_request"
        ),
        sa.CheckConstraint("observation_limit BETWEEN 1 AND 3", name="shioaji_repair_limit"),
    )
    op.create_index("ix_shioaji_member_repair_work_id", "shioaji_member_repair", ["work_id"])
    op.create_index("ix_shioaji_member_repair_plan_id", "shioaji_member_repair", ["plan_id"])
    op.create_table(
        "shioaji_repair_ack",
        sa.Column(
            "repair_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("shioaji_member_repair.repair_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    # Operators must drain grants before returning to a runtime without repair support.
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM shioaji_member_repair LIMIT 1")).first():
        raise RuntimeError("Shioaji repair evidence must be retained; downgrade is blocked")
    op.drop_table("shioaji_repair_ack")
    op.drop_table("shioaji_member_repair")
