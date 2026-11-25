"""Index ingestion runs by delivery part.

Revision ID: 1587769858c0
Revises: 64da893e5e78

Plan summaries, market freshness and unlisted-symbol reads join
``ingestion_run`` to ``daily_delivery_part`` on ``delivery_part_id``. Without an
index every such join scans all runs (about 370,000 in production on
2026-11-25, 0.2-0.5 s each), and the table only grows.

The deploy runs migrations after the writers stop, so a plain ``CREATE INDEX``
is enough. Downgrade drops the index; no data changes either way.
"""

from alembic import op

revision = "1587769858c0"
down_revision = "64da893e5e78"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_run_delivery_part", "ingestion_run", ["delivery_part_id"])


def downgrade() -> None:
    op.drop_index("idx_run_delivery_part", table_name="ingestion_run")
