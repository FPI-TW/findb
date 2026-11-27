"""Attach yearly market_data_eod partitions for 2000-2025 so history can be backfilled (#754).

Revision ID: e0649188f071
Revises: e5979e281ba9

Normalizers write EOD rows only into a migration-owned yearly partition
(`BaseNormalizer.ensure_eod_partition`), and the original partitioning created
years from the first stored row through five years ahead, so every date before
2026 failed with `EOD_PARTITION_UNAVAILABLE`. The Owner capped historical
backfill at 2000 (2026-11-27), so this attaches `market_data_eod_y2000` through
`market_data_eod_y2025`.

A new range partition cannot be attached while the default partition holds rows
in that range, so the upgrade refuses before any change if
`market_data_eod_default` has a row in 2000-2025. Downgrade drops only these
partitions and refuses while any of them holds a row: a schema rollback does not
delete backfilled history.
"""

import sqlalchemy as sa
from alembic import op

revision = "e0649188f071"
down_revision = "e5979e281ba9"
branch_labels = None
depends_on = None

FIRST_YEAR = 2000
LAST_YEAR = 2025


def _years() -> range:
    return range(FIRST_YEAR, LAST_YEAR + 1)


def upgrade() -> None:
    bind = op.get_bind()
    stray = bind.scalar(
        sa.text(
            "SELECT count(*) FROM market_data_eod_default "
            f"WHERE trade_date >= DATE '{FIRST_YEAR}-01-01' "
            f"AND trade_date < DATE '{LAST_YEAR + 1}-01-01'"
        )
    )
    if stray:
        raise RuntimeError(
            f"market_data_eod_default holds {stray} rows in {FIRST_YEAR}-{LAST_YEAR}; "
            "move them out before attaching the yearly partitions"
        )
    for year in _years():
        op.execute(
            f"CREATE TABLE IF NOT EXISTS market_data_eod_y{year} PARTITION OF market_data_eod "
            f"FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')"
        )


def downgrade() -> None:
    bind = op.get_bind()
    held = [
        year
        for year in _years()
        if bind.scalar(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"market_data_eod_y{year}"}
        )
        and bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM market_data_eod_y{year})"))
    ]
    if held:
        raise RuntimeError(
            f"market_data_eod partitions for {held} hold backfilled rows; "
            "a schema rollback does not delete history"
        )
    for year in _years():
        op.execute(f"DROP TABLE IF EXISTS market_data_eod_y{year}")
