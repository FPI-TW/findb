"""phase_2_runtime_split_and_stats

Revision ID: c4d5e6f7a8b9
Revises: b3a7c9d2e4f1
Create Date: 2026-07-09 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b3a7c9d2e4f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "instrument_stats",
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_trade_date", sa.Date(), nullable=True),
        sa.Column("latest_trade_date", sa.Date(), nullable=True),
        sa.Column("latest_price", sa.NUMERIC(20, 8), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_instrument_stats_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instrument_id", name=op.f("pk_instrument_stats")),
    )

    op.execute(
        sa.text("""
        INSERT INTO instrument_stats (
            instrument_id,
            first_trade_date,
            latest_trade_date,
            latest_price,
            updated_at
        )
        SELECT
            i.instrument_id,
            LEAST(e.first_trade_date, f.first_trade_date) AS first_trade_date,
            e.latest_trade_date,
            latest_eod.close AS latest_price,
            now() AS updated_at
        FROM instruments i
        LEFT JOIN (
            SELECT
                instrument_id,
                min(trade_date) AS first_trade_date,
                max(trade_date) AS latest_trade_date
            FROM market_data_eod
            GROUP BY instrument_id
        ) e ON e.instrument_id = i.instrument_id
        LEFT JOIN LATERAL (
            SELECT close
            FROM market_data_eod
            WHERE instrument_id = i.instrument_id
              AND trade_date = e.latest_trade_date
            LIMIT 1
        ) latest_eod ON true
        LEFT JOIN (
            SELECT instrument_id, min(trade_date) AS first_trade_date
            FROM futures_continuous_eod
            GROUP BY instrument_id
        ) f ON f.instrument_id = i.instrument_id
        WHERE e.first_trade_date IS NOT NULL
           OR f.first_trade_date IS NOT NULL
        """)
    )


def downgrade() -> None:
    op.drop_table("instrument_stats")
