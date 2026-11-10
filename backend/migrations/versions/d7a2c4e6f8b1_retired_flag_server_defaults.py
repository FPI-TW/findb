"""Give the two retired full-market flag columns server defaults.

Revision ID: d7a2c4e6f8b1
Revises: c3f6a9d1e2b7

``full_market_environment.enabled`` and ``full_market_enrollment.reported_enabled``
have no readers since the kill switch was retired, and no code sets them. These
defaults let the next release drop both columns from the ORM before dropping them
from the schema. Existing values are unchanged.
"""

import sqlalchemy as sa
from alembic import op

revision = "d7a2c4e6f8b1"
down_revision = "c3f6a9d1e2b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("full_market_environment", "enabled", server_default=sa.text("false"))
    op.alter_column("full_market_enrollment", "reported_enabled", server_default=sa.text("true"))


def downgrade() -> None:
    op.alter_column("full_market_enrollment", "reported_enabled", server_default=None)
    op.alter_column("full_market_environment", "enabled", server_default=None)
