"""Persist observed provider-account cooldown independently of stable work.

Revision ID: c64eb01f8a32
Revises: b53da90e7f21
"""

import sqlalchemy as sa
from alembic import op

revision = "c64eb01f8a32"
down_revision = "b53da90e7f21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_work_owner",
        sa.Column("acquisition_not_before", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_work_owner", "acquisition_not_before")
