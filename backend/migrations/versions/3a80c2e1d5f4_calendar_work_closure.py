"""Retain audited calendar closures without rewriting frozen evidence.

Revision ID: 3a80c2e1d5f4
Revises: 2f79b1d0c4e3
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "3a80c2e1d5f4"
down_revision = "2f79b1d0c4e3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "provider_work_calendar_closure",
        sa.Column(
            "work_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("provider_work.work_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("closure_id", pg.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column(
            "plan_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("daily_delivery_plan.plan_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("execution_profile", sa.String(20), nullable=False),
        sa.Column("actor", sa.String(300), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM provider_work_calendar_closure LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Calendar closure evidence must be retained; downgrade is blocked")
    op.drop_table("provider_work_calendar_closure")
