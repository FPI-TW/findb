"""phase_3_api_keys

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-09 15:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_key",
        sa.Column("key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("tier", sa.String(length=30), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rate_limit_requests", sa.Integer(), nullable=False),
        sa.Column("rate_limit_window", sa.Integer(), nullable=False),
        sa.Column("page_size_limit", sa.Integer(), nullable=False),
        sa.Column("usage_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key_id", name=op.f("pk_api_key")),
        sa.UniqueConstraint("key_hash", name="uq_api_key_hash"),
    )
    op.create_index("idx_api_key_owner", "api_key", ["owner"], unique=False)
    op.create_index("idx_api_key_revoked", "api_key", ["revoked_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_api_key_revoked", table_name="api_key")
    op.drop_index("idx_api_key_owner", table_name="api_key")
    op.drop_table("api_key")
