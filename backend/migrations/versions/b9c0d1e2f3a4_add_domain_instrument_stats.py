"""add explicit EOD and minute instrument read statistics

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b9c0d1e2f3a4"
down_revision: Union[str, Sequence[str], None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for column in (
        sa.Column("eod_first_date", sa.Date(), nullable=True),
        sa.Column("eod_latest_date", sa.Date(), nullable=True),
        sa.Column("eod_latest_close", sa.Numeric(20, 8), nullable=True),
        sa.Column("minute_first_bar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("minute_latest_bar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("minute_latest_close", sa.Numeric(20, 8), nullable=True),
    ):
        op.add_column("instrument_stats", column)

    # The legacy first_trade_date column was originally backfilled with the
    # earliest value from EOD *or* continuous futures. Recompute both domains
    # from their canonical tables instead of copying that mixed projection.
    # LATERAL first/latest probes use the existing per-instrument ordering
    # indexes and avoid an unbounded GROUP BY over the partitioned tables.
    op.execute(
        """
        INSERT INTO instrument_stats (
            instrument_id,
            eod_first_date,
            eod_latest_date,
            eod_latest_close,
            minute_first_bar_at,
            minute_latest_bar_at,
            minute_latest_close,
            updated_at
        )
        SELECT
            i.instrument_id,
            eod_first.trade_date,
            eod_latest.trade_date,
            eod_latest.close,
            minute_first.bar_start_time,
            minute_latest.bar_start_time,
            minute_latest.close,
            now()
        FROM instruments AS i
        LEFT JOIN LATERAL (
            SELECT e.trade_date
            FROM market_data_eod AS e
            WHERE e.instrument_id = i.instrument_id
            ORDER BY e.trade_date ASC
            LIMIT 1
        ) AS eod_first ON TRUE
        LEFT JOIN LATERAL (
            SELECT e.trade_date, e.close
            FROM market_data_eod AS e
            WHERE e.instrument_id = i.instrument_id
            ORDER BY e.trade_date DESC
            LIMIT 1
        ) AS eod_latest ON TRUE
        LEFT JOIN LATERAL (
            SELECT m.bar_start_time
            FROM market_data_minute AS m
            WHERE m.instrument_id = i.instrument_id
            ORDER BY m.bar_start_time ASC
            LIMIT 1
        ) AS minute_first ON TRUE
        LEFT JOIN LATERAL (
            SELECT m.bar_start_time, m.close
            FROM market_data_minute AS m
            WHERE m.instrument_id = i.instrument_id
            ORDER BY m.bar_start_time DESC
            LIMIT 1
        ) AS minute_latest ON TRUE
        WHERE eod_first.trade_date IS NOT NULL
           OR minute_first.bar_start_time IS NOT NULL
        ON CONFLICT (instrument_id) DO UPDATE SET
            eod_first_date = EXCLUDED.eod_first_date,
            eod_latest_date = EXCLUDED.eod_latest_date,
            eod_latest_close = EXCLUDED.eod_latest_close,
            minute_first_bar_at = EXCLUDED.minute_first_bar_at,
            minute_latest_bar_at = EXCLUDED.minute_latest_bar_at,
            minute_latest_close = EXCLUDED.minute_latest_close,
            updated_at = EXCLUDED.updated_at
        """
    )


def downgrade() -> None:
    for column_name in (
        "minute_latest_close",
        "minute_latest_bar_at",
        "minute_first_bar_at",
        "eod_latest_close",
        "eod_latest_date",
        "eod_first_date",
    ):
        op.drop_column("instrument_stats", column_name)
