"""add futures continuous contract fields

Revision ID: 5da6b7c8d9e0
Revises: 4cf5a6b7c8d9
Create Date: 2026-07-22 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5da6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "4cf5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "futures_continuous_eod",
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "futures_continuous_eod",
        sa.Column("active_contract_code", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "futures_continuous_eod",
        sa.Column("roll_adjustment", sa.Numeric(precision=20, scale=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("futures_continuous_eod", "roll_adjustment")
    op.drop_column("futures_continuous_eod", "active_contract_code")
    op.drop_column("futures_continuous_eod", "open_interest")
