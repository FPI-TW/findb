"""add missing delivery alerts

Revision ID: 4cf5a6b7c8d9
Revises: 3be4f5a6b7c8
Create Date: 2026-07-22 00:00:00.000000
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4cf5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "3be4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MONITORED_DATASETS = (
    "tw_equity_eod",
    "tw_etf_eod",
    "futures_continuous_eod",
    "wtx_eod",
)


def upgrade() -> None:
    op.create_table(
        "missing_delivery_alert",
        sa.Column("alert_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_key", sa.String(50), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("schema_id", sa.String(50), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("expected_data_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_missing_delivery_alert_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_key"], ["dataset_registry.dataset_key"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("alert_id"),
        sa.UniqueConstraint(
            "dataset_key",
            "source",
            "schema_id",
            "schema_version",
            "expected_data_date",
            name="uq_missing_delivery_identity_date",
        ),
    )
    op.create_index(
        "idx_missing_delivery_status_detected",
        "missing_delivery_alert",
        ["status", "first_detected_at"],
    )
    op.create_index(
        "idx_missing_delivery_dataset_source",
        "missing_delivery_alert",
        ["dataset_key", "source"],
    )

    connection = op.get_bind()
    disabled = json.dumps({"action": "disabled", "expected_sources": []})
    for dataset_key in MONITORED_DATASETS:
        connection.execute(
            sa.text("""
                UPDATE dataset_registry
                SET config = jsonb_set(
                    config,
                    '{delivery_expectation,missing_delivery}',
                    CAST(:disabled AS jsonb),
                    true
                ), updated_at = now()
                WHERE dataset_key = :dataset_key
                  AND jsonb_typeof(config->'delivery_expectation') = 'object'
                  AND NOT (config->'delivery_expectation' ? 'missing_delivery')
            """),
            {"dataset_key": dataset_key, "disabled": disabled},
        )


def downgrade() -> None:
    # Operator configuration is backward-compatible and intentionally retained.
    op.drop_index("idx_missing_delivery_dataset_source", table_name="missing_delivery_alert")
    op.drop_index("idx_missing_delivery_status_detected", table_name="missing_delivery_alert")
    op.drop_table("missing_delivery_alert")
