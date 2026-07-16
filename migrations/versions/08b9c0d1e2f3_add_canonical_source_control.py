"""add canonical source precedence control columns

Revision ID: 08b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-07-15 17:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "08b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SOURCE_CONTROL_TABLES = (
    "market_data_eod",
    "corporate_action",
    "macro_observation",
    "futures_contract",
    "futures_continuous_eod",
)


def upgrade() -> None:
    for table_name in SOURCE_CONTROL_TABLES:
        op.add_column(
            table_name,
            sa.Column(
                "source_priority",
                sa.Integer(),
                server_default="1000",
                nullable=False,
            ),
        )
        op.add_column(
            table_name,
            sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for table_name in reversed(SOURCE_CONTROL_TABLES):
        op.drop_column(table_name, "source_fetched_at")
        op.drop_column(table_name, "source_priority")
