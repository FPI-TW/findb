"""Index provider work receipts by work and member.

Revision ID: c5e7a9b1d3f6
Revises: b4d6f8a1c3e5

The Admin queue projection and the Fetcher claim both ask, for every prepared
delivery of a work, whether a receipt with the same work and member key exists.
The table only had its ``run_id`` primary key, so each check scanned every
receipt and the cost grew with prepared rows times receipt rows. The index is
additive: a previous release ignores it, and downgrade only drops it.
"""

from alembic import op

revision = "c5e7a9b1d3f6"
down_revision = "b4d6f8a1c3e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_provider_work_receipt_member",
        "provider_work_receipt",
        ["work_id", "member_key"],
    )


def downgrade() -> None:
    op.drop_index("idx_provider_work_receipt_member", table_name="provider_work_receipt")
