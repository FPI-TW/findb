"""add durable scheduler control plane

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SCHEDULERS = (
    (
        "twelve_data_us_common_stocks_daily_v1",
        "twelve_data",
        ["us_equity_eod"],
    ),
    (
        "finlab_tw_1430_tw_equity_eod",
        "finlab",
        ["tw_equity_eod"],
    ),
    (
        "shioaji_tw_pilot_v1",
        "shioaji",
        ["tw_equity_minute", "tw_etf_minute"],
    ),
)


def upgrade() -> None:
    op.create_table(
        "scheduler_control",
        sa.Column("scheduler_key", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column(
            "dataset_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("desired_state", sa.String(length=20), nullable=False),
        sa.Column("observed_state", sa.String(length=20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cycle_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cycle_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(scheduler_key)) > 0",
            name="ck_scheduler_control_scheduler_key_nonempty",
        ),
        sa.CheckConstraint(
            "length(trim(provider)) > 0",
            name="ck_scheduler_control_provider_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(dataset_keys) = 'array' AND jsonb_array_length(dataset_keys) > 0",
            name="ck_scheduler_control_dataset_keys_array",
        ),
        sa.CheckConstraint(
            "desired_state IN ('running', 'stopped')",
            name="ck_scheduler_control_desired_state_valid",
        ),
        sa.CheckConstraint(
            "observed_state IN ('running', 'stopped')",
            name="ck_scheduler_control_observed_state_valid",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_scheduler_control_revision_positive",
        ),
        sa.PrimaryKeyConstraint("scheduler_key"),
    )
    op.create_index(
        "idx_scheduler_control_provider", "scheduler_control", ["provider"], unique=False
    )
    op.create_index(
        "idx_scheduler_control_state",
        "scheduler_control",
        ["desired_state", "observed_state"],
        unique=False,
    )
    op.create_index(
        "idx_scheduler_control_heartbeat",
        "scheduler_control",
        ["last_heartbeat_at"],
        unique=False,
    )

    insert = sa.text(
        """
        INSERT INTO scheduler_control (
            scheduler_key, provider, dataset_keys, desired_state, observed_state,
            revision, last_heartbeat_at, last_cycle_started_at,
            last_cycle_completed_at, last_error, created_at, updated_at
        ) VALUES (
            :scheduler_key, :provider, CAST(:dataset_keys AS jsonb), 'stopped', 'stopped',
            1, NULL, NULL, NULL, NULL, now(), now()
        )
        """
    )
    for scheduler_key, provider, dataset_keys in _SCHEDULERS:
        op.execute(
            insert.bindparams(
                scheduler_key=scheduler_key,
                provider=provider,
                dataset_keys=json.dumps(dataset_keys),
            )
        )


def downgrade() -> None:
    op.drop_table("scheduler_control")
