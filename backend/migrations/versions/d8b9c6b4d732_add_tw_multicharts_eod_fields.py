"""add_tw_multicharts_eod_fields

Revision ID: d8b9c6b4d732
Revises: 841728d7045e
Create Date: 2026-04-30 16:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8b9c6b4d732"
down_revision: Union[str, Sequence[str], None] = "841728d7045e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("market_data_eod", sa.Column("up_volume", sa.BigInteger(), nullable=True))
    op.add_column("market_data_eod", sa.Column("down_volume", sa.BigInteger(), nullable=True))
    op.add_column("market_data_eod", sa.Column("up_ticks", sa.BigInteger(), nullable=True))
    op.add_column("market_data_eod", sa.Column("down_ticks", sa.BigInteger(), nullable=True))
    op.add_column("market_data_eod", sa.Column("total_ticks", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("market_data_eod", "total_ticks")
    op.drop_column("market_data_eod", "down_ticks")
    op.drop_column("market_data_eod", "up_ticks")
    op.drop_column("market_data_eod", "down_volume")
    op.drop_column("market_data_eod", "up_volume")
