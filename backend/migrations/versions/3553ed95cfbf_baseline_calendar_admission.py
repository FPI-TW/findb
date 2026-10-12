"""Allow baseline/calendar admissions without legacy runtime enrollment.

Revision ID: 3553ed95cfbf
Revises: 2442dc84beae
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "3553ed95cfbf"
down_revision = "2442dc84beae"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "full_market_admission",
        "enrollment_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM full_market_admission WHERE enrollment_id IS NULL)")
    ):
        raise RuntimeError("baseline/calendar admissions require the current schema")
    op.alter_column(
        "full_market_admission",
        "enrollment_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
