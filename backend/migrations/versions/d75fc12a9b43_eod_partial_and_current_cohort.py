"""Expand historical EOD partial outcomes and immutable current-cohort snapshots.

Revision ID: d75fc12a9b43
Revises: c64eb01f8a32
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d75fc12a9b43"
down_revision = "c64eb01f8a32"
branch_labels = None
depends_on = None


def _constraints(*, partial: bool) -> None:
    for table, name, statuses in (
        (
            "historical_backfill_request",
            "historical_backfill_request_status_valid",
            ["queued", "running", "completed", "failed", "cancelled"],
        ),
        (
            "historical_backfill_item",
            "historical_backfill_item_status_valid",
            ["pending", "running", "completed", "failed", "cancelled", "skipped_calendar"],
        ),
    ):
        op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
        if partial:
            statuses.append("partial")
        values = ", ".join(f"'{status}'" for status in statuses)
        op.create_check_constraint(name, table, f"status IN ({values})")


def upgrade() -> None:
    op.add_column(
        "historical_backfill_request",
        sa.Column("universe_snapshot", postgresql.JSONB(), nullable=True),
    )
    _constraints(partial=True)


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM historical_backfill_request "
            "WHERE universe_snapshot IS NOT NULL OR status='partial') "
            "OR EXISTS (SELECT 1 FROM historical_backfill_item WHERE status='partial') "
            "OR EXISTS (SELECT 1 FROM provider_work WHERE state='partial')"
            " OR EXISTS (SELECT 1 FROM ingestion_run WHERE schema_id='market_eod' AND schema_version=2)"
            " OR EXISTS (SELECT 1 FROM dataset_registry WHERE config->'hk_provider_cutover' IS NOT NULL)"
        )
    )
    if used:
        raise RuntimeError(
            "EOD v2 history exists; preserve cohort and partial evidence before rollback"
        )
    _constraints(partial=False)
    op.drop_column("historical_backfill_request", "universe_snapshot")
