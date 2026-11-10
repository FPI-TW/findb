"""Record each universe member's exchange listing date.

Revision ID: b5d8e2f4a6c1
Revises: a7c3e1f9d2b4

Expand-only: the column is nullable and existing members keep NULL, which plan
creation treats as already listed, so frozen releases, plans and digests are
unchanged. Fetcher may send ``listed_date`` only after this revision is live.
"""

import sqlalchemy as sa
from alembic import op

revision = "b5d8e2f4a6c1"
down_revision = "a7c3e1f9d2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("universe_member", sa.Column("listed_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("universe_member", "listed_date")
