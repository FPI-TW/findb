"""provider_historical_backfill

Revision ID: a8b9c0d1e2f3
Revises: d6e7f8a9b0c1
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "historical_backfill_request",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_key", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column(
            "dataset_key",
            sa.String(50),
            sa.ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("cancelled_by", sa.String(100), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(50), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("request_key", name="uq_historical_backfill_request_key"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="historical_backfill_request_status_valid",
        ),
        sa.CheckConstraint("start_date <= end_date", name="historical_backfill_request_date_order"),
    )
    op.create_index(
        "idx_historical_backfill_request_provider_status",
        "historical_backfill_request",
        ["provider", "status"],
    )
    op.create_table(
        "historical_backfill_item",
        sa.Column("item_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("historical_backfill_request.request_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(50), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("request_id", "trade_date", name="uq_historical_backfill_item_date"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="historical_backfill_item_status_valid",
        ),
    )
    op.create_index(
        "idx_historical_backfill_item_claim", "historical_backfill_item", ["status", "trade_date"]
    )
    op.create_index(
        "idx_historical_backfill_item_lease_expiry",
        "historical_backfill_item",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_historical_backfill_item_lease_expiry", table_name="historical_backfill_item"
    )
    op.drop_index("idx_historical_backfill_item_claim", table_name="historical_backfill_item")
    op.drop_table("historical_backfill_item")
    op.drop_index(
        "idx_historical_backfill_request_provider_status", table_name="historical_backfill_request"
    )
    op.drop_table("historical_backfill_request")
