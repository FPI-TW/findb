"""Record universe members' exchange listing dates.

Revision ID: b5d8e2f4a6c1
Revises: a7c3e1f9d2b4

Expand-only: a new table holds a listing date only for members whose official
source states one. Existing members have no row, which plan creation treats as
already listed, so frozen releases, plans and digests are unchanged. Fetcher may
send ``listed_date`` only after this revision is live.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b5d8e2f4a6c1"
down_revision = "a7c3e1f9d2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "universe_member_listing",
        sa.Column("member_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("listed_date", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["universe_member.member_id"], ondelete="RESTRICT"),
    )


def downgrade() -> None:
    op.drop_table("universe_member_listing")
