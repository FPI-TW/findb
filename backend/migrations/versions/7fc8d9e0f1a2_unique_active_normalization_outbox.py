"""enforce one active normalization outbox row per job

Revision ID: 7fc8d9e0f1a2
Revises: 6eb7c8d9e0f1
Create Date: 2026-07-28 12:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7fc8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "6eb7c8d9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_normalization_outbox_active_job"


def upgrade() -> None:
    op.execute(
        sa.text("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM normalization_outbox
                WHERE status IN ('pending', 'publishing')
                GROUP BY job_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot enforce one active normalization outbox row per job: '
                    'duplicate pending/publishing rows exist';
            END IF;
        END
        $$;
    """)
    )
    op.create_index(
        INDEX_NAME,
        "normalization_outbox",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'publishing')"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="normalization_outbox")
