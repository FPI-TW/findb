"""Index retry-exhausted runs for the queue health view.

Revision ID: 208a9707fe3f
Revises: c7e9a1b3d5f8

The Dashboard polls queue health every minute. Counting
``ingestion_run.failure_code = 'RETRY_EXHAUSTED'`` scanned every run (about
370,000 in production on 2026-11-25, 0.3 s per poll). A partial index holds only
those rare runs, so the count reads a handful of entries.

The deploy runs migrations after the writers stop, so a plain ``CREATE INDEX``
is enough. Downgrade drops the index; no data changes either way.
"""

import sqlalchemy as sa
from alembic import op

revision = "208a9707fe3f"
down_revision = "c7e9a1b3d5f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_run_retry_exhausted",
        "ingestion_run",
        ["failure_code"],
        postgresql_where=sa.text("failure_code = 'RETRY_EXHAUSTED'"),
    )


def downgrade() -> None:
    op.drop_index("idx_run_retry_exhausted", table_name="ingestion_run")
