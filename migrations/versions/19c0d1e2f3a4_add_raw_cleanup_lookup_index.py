"""add raw cleanup lookup index

Revision ID: 19c0d1e2f3a4
Revises: 08b9c0d1e2f3
Create Date: 2026-07-17 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "19c0d1e2f3a4"
down_revision: Union[str, Sequence[str], None] = "08b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "idx_run_raw_payload"


def upgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "ingestion_run",
        ["raw_payload_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="ingestion_run")
