"""configure US equity ingress contract

Revision ID: 6eb7c8d9e0f1
Revises: 5da6b7c8d9e0
Create Date: 2026-07-24 00:00:00.000000
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "6eb7c8d9e0f1"
down_revision: Union[str, Sequence[str], None] = "5da6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DATASET_KEY = "us_equity_eod"
CONTRACT_CONFIG = {
    "schema_id": "market_eod",
    "accepted_schema_versions": [1],
    "current_schema_version": 1,
    "schema_enforcement": "audit",
    "defaults": {
        "market": "US",
        "asset_class": "equity",
        "currency": "USD",
    },
    "delivery_expectation": {
        "delivery_mode": "incremental",
        "baseline": {
            "strategy": "rolling_median",
            "scope": "dataset_source_schema",
            "window_size": 7,
            "minimum_history": 3,
        },
        "record_count": {
            "minimum_record_count": 1,
            "maximum_count_drop_ratio": 0.0,
            "action": "warn",
        },
        "freshness": {
            "maximum_fetch_age_hours": 36,
            "allowed_clock_skew_minutes": 5,
            "action": "warn",
        },
        "latest_date": {
            "calendar_market": "US",
            "timezone": "America/New_York",
            "market_close_time": "16:00:00",
            "availability_grace_minutes": 120,
            "action": "warn",
        },
    },
}


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("""
            UPDATE dataset_registry
            SET config =
                CAST(:contract_config AS jsonb)
                || COALESCE(config, '{}'::jsonb)
                || jsonb_build_object(
                    'defaults',
                    CAST(:contract_config AS jsonb)->'defaults'
                    || CASE
                        WHEN jsonb_typeof(config->'defaults') = 'object'
                        THEN config->'defaults'
                        ELSE '{}'::jsonb
                    END,
                    'delivery_expectation',
                    CAST(:contract_config AS jsonb)->'delivery_expectation'
                    || CASE
                        WHEN jsonb_typeof(config->'delivery_expectation') = 'object'
                        THEN config->'delivery_expectation'
                        ELSE '{}'::jsonb
                    END
                ),
                updated_at = now()
            WHERE dataset_key = :dataset_key
            """),
        {
            "dataset_key": DATASET_KEY,
            "contract_config": json.dumps(CONTRACT_CONFIG),
        },
    )


def downgrade() -> None:
    # Contract metadata is backward-compatible and may contain operator-owned
    # overrides after deployment, so it is intentionally retained.
    pass
