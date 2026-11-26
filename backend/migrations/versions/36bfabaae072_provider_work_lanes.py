"""Give provider work and its owner an execution lane (#691 step 2a).

Revision ID: 36bfabaae072
Revises: 7733f9692fe6

A provider's work used to share one FIFO and one owner. A lane splits them: each
(environment, provider, lane) has its own owner and FIFO, so a long lane (FinLab's
intraday feeds) no longer holds the provider's other work. Every existing work
and owner belongs to ``default``, the lane every provider keeps; nothing changes
until the catalog declares another lane.

Downgrade refuses once any work or owner uses another lane, because folding two
FIFOs and two owners back into one cannot be done safely by a schema rollback.
"""

import sqlalchemy as sa
from alembic import op

revision = "36bfabaae072"
down_revision = "7733f9692fe6"
branch_labels = None
depends_on = None

LANE_CHECK = "lane ~ '^[a-z][a-z0-9_]{0,29}$'"
# Full names under the metadata naming convention (ck_<table>_<name>).
WORK_CHECK = "ck_provider_work_provider_work_lane_valid"
OWNER_CHECK = "ck_provider_work_owner_provider_work_owner_lane_valid"


def upgrade() -> None:
    op.add_column(
        "provider_work",
        sa.Column("lane", sa.String(30), nullable=False, server_default="default"),
    )
    op.create_check_constraint(op.f(WORK_CHECK), "provider_work", LANE_CHECK)
    op.drop_index("idx_provider_work_scope", table_name="provider_work")
    op.create_index(
        "idx_provider_work_scope", "provider_work", ["environment", "provider", "lane", "state"]
    )
    op.add_column(
        "provider_work_owner",
        sa.Column("lane", sa.String(30), nullable=False, server_default="default"),
    )
    op.create_check_constraint(op.f(OWNER_CHECK), "provider_work_owner", LANE_CHECK)
    op.drop_constraint("pk_provider_work_owner", "provider_work_owner", type_="primary")
    op.create_primary_key(
        "pk_provider_work_owner", "provider_work_owner", ["environment", "provider", "lane"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("provider_work", "provider_work_owner"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE lane <> 'default' LIMIT 1")).first():
            raise RuntimeError(f"{table} uses a non-default lane; downgrade is blocked")
    op.drop_constraint("pk_provider_work_owner", "provider_work_owner", type_="primary")
    op.create_primary_key(
        "pk_provider_work_owner", "provider_work_owner", ["environment", "provider"]
    )
    op.drop_constraint(op.f(OWNER_CHECK), "provider_work_owner", type_="check")
    op.drop_column("provider_work_owner", "lane")
    op.drop_index("idx_provider_work_scope", table_name="provider_work")
    op.create_index(
        "idx_provider_work_scope", "provider_work", ["environment", "provider", "state"]
    )
    op.drop_constraint(op.f(WORK_CHECK), "provider_work", type_="check")
    op.drop_column("provider_work", "lane")
