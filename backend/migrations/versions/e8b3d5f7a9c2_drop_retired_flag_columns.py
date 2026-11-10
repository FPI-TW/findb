"""Drop the two retired full-market flag columns.

Revision ID: e8b3d5f7a9c2
Revises: d7a2c4e6f8b1

``full_market_environment.enabled`` and ``full_market_enrollment.reported_enabled``
have had no readers since the kill switch was retired, and the previous release
stopped mapping them; inserts there use the d7a2c4e6f8b1 server defaults.
Downgrade restores both columns with those defaults; the dropped values are not
recoverable and no release reads them.
"""

import sqlalchemy as sa
from alembic import op

revision = "e8b3d5f7a9c2"
down_revision = "d7a2c4e6f8b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("full_market_enrollment", "reported_enabled")
    op.drop_column("full_market_environment", "enabled")


def downgrade() -> None:
    op.add_column(
        "full_market_environment",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "full_market_enrollment",
        sa.Column("reported_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
