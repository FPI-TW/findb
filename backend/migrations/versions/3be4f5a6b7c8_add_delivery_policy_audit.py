"""add delivery policy audit

Revision ID: 3be4f5a6b7c8
Revises: 2ad3e4f5a6b7
Create Date: 2026-07-22 00:00:00.000000
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3be4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "2ad3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


POLICIES = {
    "tw_equity_eod": {
        "delivery_mode": "full_snapshot",
        "baseline": {
            "strategy": "rolling_median",
            "scope": "dataset_source_schema",
            "window_size": 7,
            "minimum_history": 3,
        },
        "record_count": {
            "minimum_record_count": 2100,
            "maximum_count_drop_ratio": 0.1,
            "action": "warn",
        },
        "freshness": {
            "maximum_fetch_age_hours": 36,
            "allowed_clock_skew_minutes": 5,
            "action": "warn",
        },
        "latest_date": {
            "calendar_market": "TW",
            "timezone": "Asia/Taipei",
            "market_close_time": "13:30:00",
            "availability_grace_minutes": 120,
            "action": "warn",
        },
    },
    "tw_etf_eod": {
        "delivery_mode": "full_snapshot",
        "baseline": {
            "strategy": "rolling_median",
            "scope": "dataset_source_schema",
            "window_size": 7,
            "minimum_history": 3,
        },
        "record_count": {
            "minimum_record_count": 190,
            "maximum_count_drop_ratio": 0.1,
            "action": "warn",
        },
        "freshness": {
            "maximum_fetch_age_hours": 36,
            "allowed_clock_skew_minutes": 5,
            "action": "warn",
        },
        "latest_date": {
            "calendar_market": "TW",
            "timezone": "Asia/Taipei",
            "market_close_time": "13:30:00",
            "availability_grace_minutes": 120,
            "action": "warn",
        },
    },
    "futures_continuous_eod": {
        "delivery_mode": "full_snapshot",
        "baseline": {
            "strategy": "rolling_median",
            "scope": "dataset_source_schema",
            "window_size": 7,
            "minimum_history": 3,
        },
        "record_count": {
            "minimum_record_count": 1,
            "maximum_count_drop_ratio": 0.5,
            "action": "warn",
        },
        "freshness": {
            "maximum_fetch_age_hours": 36,
            "allowed_clock_skew_minutes": 5,
            "action": "warn",
        },
        "latest_date": {
            "calendar_market": "WTX",
            "timezone": "Asia/Taipei",
            "market_close_time": "13:45:00",
            "availability_grace_minutes": 120,
            "action": "warn",
        },
    },
    "wtx_eod": {
        "delivery_mode": "full_snapshot",
        "baseline": {
            "strategy": "rolling_median",
            "scope": "dataset_source_schema",
            "window_size": 7,
            "minimum_history": 3,
        },
        "record_count": {
            "minimum_record_count": 1,
            "maximum_count_drop_ratio": 0.5,
            "action": "warn",
        },
        "freshness": {
            "maximum_fetch_age_hours": 36,
            "allowed_clock_skew_minutes": 5,
            "action": "warn",
        },
        "latest_date": {
            "calendar_market": "WTX",
            "timezone": "Asia/Taipei",
            "market_close_time": "13:45:00",
            "availability_grace_minutes": 120,
            "action": "warn",
        },
    },
}


def upgrade() -> None:
    op.add_column(
        "ingestion_attempt",
        sa.Column("failure_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("ingestion_run", sa.Column("batch_data_date", sa.Date(), nullable=True))
    op.add_column("ingestion_run", sa.Column("delivery_mode", sa.String(20), nullable=True))
    op.add_column("ingestion_run", sa.Column("policy_outcome", sa.String(10), nullable=True))
    op.add_column(
        "ingestion_run",
        sa.Column("policy_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "ingestion_run",
        sa.Column("is_rerun", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index(
        "idx_run_delivery_policy_baseline",
        "ingestion_run",
        [
            "dataset_key",
            "source",
            "schema_id",
            "schema_version",
            "status",
            "policy_outcome",
            "delivery_mode",
            "is_rerun",
            "batch_data_date",
        ],
        unique=False,
    )

    connection = op.get_bind()
    for dataset_key, policy in POLICIES.items():
        connection.execute(
            sa.text("""
                UPDATE dataset_registry
                SET config = jsonb_set(
                    COALESCE(config, '{}'::jsonb),
                    '{delivery_expectation}',
                    CAST(:policy AS jsonb) ||
                        CASE
                            WHEN jsonb_typeof(config->'delivery_expectation') = 'object'
                            THEN config->'delivery_expectation'
                            ELSE '{}'::jsonb
                        END,
                    true
                ),
                updated_at = now()
                WHERE dataset_key = :dataset_key
            """),
            {"dataset_key": dataset_key, "policy": json.dumps(policy)},
        )


def downgrade() -> None:
    # Dataset policy metadata is intentionally retained: it is backward-compatible
    # configuration and may contain operator-owned overrides.
    op.drop_index("idx_run_delivery_policy_baseline", table_name="ingestion_run")
    op.drop_column("ingestion_run", "is_rerun")
    op.drop_column("ingestion_run", "policy_details")
    op.drop_column("ingestion_run", "policy_outcome")
    op.drop_column("ingestion_run", "delivery_mode")
    op.drop_column("ingestion_run", "batch_data_date")
    op.drop_column("ingestion_attempt", "failure_details")
