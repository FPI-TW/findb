"""Preserve whole-day TAIFEX historical completion evidence.

Revision ID: a42c89d3e610
Revises: 91a4c7e2d630
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a42c89d3e610"
down_revision = "91a4c7e2d630"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "historical_backfill_item",
        sa.Column("result_evidence", postgresql.JSONB(), nullable=True),
    )


def downgrade():
    raise RuntimeError("Historical evidence must be retained; use a forward fix")
