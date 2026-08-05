"""Promote scheduler definitions and normalized dataset scope to the control plane.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the immutable scheduler definition without changing mutable state."""
    op.add_column(
        "scheduler_control",
        sa.Column("slot_id", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "scheduler_control",
        sa.Column("scheduled_local_time", sa.Time(), nullable=True),
    )
    op.add_column(
        "scheduler_control",
        sa.Column("timezone", sa.String(length=64), nullable=True),
    )

    # The original migration intentionally persisted only provider and JSON
    # dataset scope.  Do not infer scheduler authority from DatasetRegistry's
    # delivery-expectation schedule blocks: unknown, duplicated, or widened
    # legacy mappings must stop this migration instead of being guessed.
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE row_count integer;
            BEGIN
                SELECT count(*) INTO row_count FROM scheduler_control;
                IF row_count <> 3 THEN
                    RAISE EXCEPTION
                        'scheduler definition backfill requires exactly the three legacy rows';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM scheduler_control
                    WHERE scheduler_key NOT IN (
                        'twelve_data_us_common_stocks_daily_v1',
                        'finlab_tw_1430_tw_equity_eod',
                        'shioaji_tw_pilot_v1'
                    )
                ) THEN
                    RAISE EXCEPTION 'unknown legacy scheduler mapping';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM scheduler_control
                    WHERE NOT (
                        (
                            scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                            AND provider = 'twelve_data'
                            AND dataset_keys = '["us_equity_eod"]'::jsonb
                        ) OR (
                            scheduler_key = 'finlab_tw_1430_tw_equity_eod'
                            AND provider = 'finlab'
                            AND dataset_keys = '["tw_equity_eod"]'::jsonb
                        ) OR (
                            scheduler_key = 'shioaji_tw_pilot_v1'
                            AND provider = 'shioaji'
                            AND dataset_keys = '["tw_equity_minute", "tw_etf_minute"]'::jsonb
                        )
                    )
                ) THEN
                    RAISE EXCEPTION 'invalid legacy scheduler provider or dataset mapping';
                END IF;
            END
            $$;
            """
        )
    )

    # A fresh install can reach c1 before the post-migration dataset seed runs.
    # Materialize only the two legacy EOD FK targets needed by the scheduler
    # association.  ``DO NOTHING`` is deliberate: an operator-owned registry
    # row (including its config and timestamps) must never be overwritten.
    op.execute(
        sa.text(
            """
            INSERT INTO dataset_registry (
                dataset_key, name, description, asset_class, market, frequency,
                is_active, config, created_at, updated_at
            )
            VALUES
                (
                    'us_equity_eod',
                    'US equity EOD',
                    'US equity end-of-day data',
                    'equity',
                    'US',
                    'daily',
                    true,
                    '{"source_format":"bloomberg_equity_api"}'::jsonb,
                    now(),
                    now()
                ),
                (
                    'tw_equity_eod',
                    'TW equity EOD',
                    'TW equity end-of-day data',
                    'equity',
                    'TW',
                    'daily',
                    true,
                    '{"source_format":"finlab_twstock_direct"}'::jsonb,
                    now(),
                    now()
                )
            ON CONFLICT (dataset_key) DO NOTHING
            """
        )
    )

    op.execute(
        sa.text(
            """
            UPDATE scheduler_control
            SET slot_id = CASE scheduler_key
                    WHEN 'twelve_data_us_common_stocks_daily_v1' THEN 'us_0600'
                    WHEN 'finlab_tw_1430_tw_equity_eod' THEN 'tw_1430'
                    WHEN 'shioaji_tw_pilot_v1' THEN 'tw_1430'
                END,
                scheduled_local_time = CASE scheduler_key
                    WHEN 'twelve_data_us_common_stocks_daily_v1' THEN TIME '06:00:00'
                    WHEN 'finlab_tw_1430_tw_equity_eod' THEN TIME '14:30:00'
                    WHEN 'shioaji_tw_pilot_v1' THEN TIME '14:30:00'
                END,
                timezone = 'Asia/Taipei'
            """
        )
    )
    op.alter_column("scheduler_control", "slot_id", nullable=False)
    op.alter_column("scheduler_control", "scheduled_local_time", nullable=False)
    op.alter_column("scheduler_control", "timezone", nullable=False)
    op.create_check_constraint(
        "ck_scheduler_control_slot_id_nonempty",
        "scheduler_control",
        "length(trim(slot_id)) > 0",
    )
    op.create_check_constraint(
        "ck_scheduler_control_timezone_nonempty",
        "scheduler_control",
        "length(trim(timezone)) > 0",
    )

    op.create_table(
        "scheduler_dataset",
        sa.Column("scheduler_key", sa.String(length=100), nullable=False),
        sa.Column("dataset_key", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(
            ["scheduler_key"],
            ["scheduler_control.scheduler_key"],
            name="fk_scheduler_dataset_scheduler_key",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_key"],
            ["dataset_registry.dataset_key"],
            name="fk_scheduler_dataset_dataset_key",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("scheduler_key", "dataset_key"),
    )
    op.create_index(
        "idx_scheduler_dataset_dataset", "scheduler_dataset", ["dataset_key"], unique=False
    )

    # Copy only the already persisted compatibility projection.  The FK keeps
    # an invalid dataset key from being silently accepted.
    op.execute(
        sa.text(
            """
            INSERT INTO scheduler_dataset (scheduler_key, dataset_key)
            SELECT scheduler_key, value
            FROM scheduler_control
            CROSS JOIN LATERAL jsonb_array_elements_text(dataset_keys) AS items(value)
            """
        )
    )


def downgrade() -> None:
    """Remove only this migration's definition and normalized scope objects."""
    op.drop_index("idx_scheduler_dataset_dataset", table_name="scheduler_dataset")
    op.drop_table("scheduler_dataset")
    op.drop_constraint("ck_scheduler_control_timezone_nonempty", "scheduler_control", type_="check")
    op.drop_constraint("ck_scheduler_control_slot_id_nonempty", "scheduler_control", type_="check")
    op.drop_column("scheduler_control", "timezone")
    op.drop_column("scheduler_control", "scheduled_local_time")
    op.drop_column("scheduler_control", "slot_id")
