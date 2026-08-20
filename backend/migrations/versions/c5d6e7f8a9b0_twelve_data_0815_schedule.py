"""Move the governed Twelve Data run to 08:15 Taipei.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9

The scheduler definition and dataset delivery policy must change together.
Upgrade therefore validates the complete known Twelve Data control,
normalized mapping, and registry policy before changing any governed field.
Unrelated operator JSON is preserved.  Downgrade is deliberately conservative:
it only reverts an untouched 08:15/09:15 pair and otherwise leaves both sides
unchanged.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _validate_upgrade_shape() -> None:
    """Abort before mutation when the governed Twelve Data state drifted."""

    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM scheduler_control
                    WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                      AND provider = 'twelve_data'
                      AND slot_id = 'western_markets_window'
                      AND scheduled_local_time = TIME '10:30:00'
                      AND timezone = 'Asia/Taipei'
                      AND dataset_keys = '["us_equity_eod"]'::jsonb
                ) THEN
                    RAISE EXCEPTION
                        'Twelve Data scheduler definition drifted; migration aborted';
                END IF;

                IF (
                    SELECT count(*)
                    FROM scheduler_dataset
                    WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                ) <> 1 OR NOT EXISTS (
                    SELECT 1
                    FROM scheduler_dataset
                    WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                      AND dataset_key = 'us_equity_eod'
                ) THEN
                    RAISE EXCEPTION
                        'Twelve Data scheduler dataset mapping drifted; migration aborted';
                END IF;

                IF NOT EXISTS (
                    SELECT 1
                    FROM dataset_registry
                    WHERE dataset_key = 'us_equity_eod'
                      AND jsonb_typeof(config) = 'object'
                      AND jsonb_typeof(config->'delivery_expectation') = 'object'
                      AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
                      AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                            = '["twelve_data"]'::jsonb
                      AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                            = '11:30:00'
                      AND jsonb_typeof(config#>'{delivery_expectation,schedule}') = 'object'
                      AND config#>>'{delivery_expectation,schedule,enabled}' = 'true'
                      AND config#>>'{delivery_expectation,schedule,slot_id}'
                            = 'western_markets_window'
                      AND config#>>'{delivery_expectation,schedule,local_time}' = '10:30:00'
                      AND config#>>'{delivery_expectation,schedule,timezone}' = 'Asia/Taipei'
                      AND config#>'{delivery_expectation,schedule,expected_sources}'
                            = '["twelve_data"]'::jsonb
                      AND config#>>'{delivery_expectation,schedule,target_date_lag_days}' = '1'
                ) THEN
                    RAISE EXCEPTION
                        'Twelve Data delivery schedule drifted; migration aborted';
                END IF;
            END $$
            """
        )
    )


def upgrade() -> None:
    _validate_upgrade_shape()

    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                scheduler_rows integer;
                policy_rows integer;
            BEGIN
                -- Change the scheduler only when the complete prior governed
                -- pair, including normalized scope, is still present.
                UPDATE scheduler_control
                SET scheduled_local_time = TIME '08:15:00',
                    updated_at = now()
                WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                  AND provider = 'twelve_data'
                  AND slot_id = 'western_markets_window'
                  AND scheduled_local_time = TIME '10:30:00'
                  AND timezone = 'Asia/Taipei'
                  AND dataset_keys = '["us_equity_eod"]'::jsonb
                  AND (
                      SELECT count(*)
                      FROM scheduler_dataset
                      WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                  ) = 1
                  AND EXISTS (
                      SELECT 1
                      FROM scheduler_dataset
                      WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        AND dataset_key = 'us_equity_eod'
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM dataset_registry
                      WHERE dataset_key = 'us_equity_eod'
                        AND jsonb_typeof(config) = 'object'
                        AND jsonb_typeof(config->'delivery_expectation') = 'object'
                        AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
                        AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                              = '["twelve_data"]'::jsonb
                        AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                              = '11:30:00'
                        AND jsonb_typeof(config#>'{delivery_expectation,schedule}') = 'object'
                        AND config#>>'{delivery_expectation,schedule,enabled}' = 'true'
                        AND config#>>'{delivery_expectation,schedule,slot_id}'
                              = 'western_markets_window'
                        AND config#>>'{delivery_expectation,schedule,local_time}' = '10:30:00'
                        AND config#>>'{delivery_expectation,schedule,timezone}' = 'Asia/Taipei'
                        AND config#>'{delivery_expectation,schedule,expected_sources}'
                              = '["twelve_data"]'::jsonb
                        AND config#>>'{delivery_expectation,schedule,target_date_lag_days}' = '1'
                  );
                GET DIAGNOSTICS scheduler_rows = ROW_COUNT;

                IF scheduler_rows <> 1 THEN
                    RAISE EXCEPTION
                        'Twelve Data scheduler update did not affect exactly one row';
                END IF;

                -- Re-check the complete pair after the first mutation.  A
                -- policy-step failure rolls back this scheduler update too.
                UPDATE dataset_registry
                SET config = jsonb_set(
                        jsonb_set(
                            config,
                            '{delivery_expectation,schedule,local_time}',
                            '"08:15:00"'::jsonb,
                            true
                        ),
                        '{delivery_expectation,missing_delivery,deadline_local_time}',
                        '"09:15:00"'::jsonb,
                        true
                    ),
                    updated_at = now()
                WHERE dataset_key = 'us_equity_eod'
                  AND jsonb_typeof(config) = 'object'
                  AND jsonb_typeof(config->'delivery_expectation') = 'object'
                  AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
                  AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                        = '["twelve_data"]'::jsonb
                  AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                        = '11:30:00'
                  AND jsonb_typeof(config#>'{delivery_expectation,schedule}') = 'object'
                  AND config#>>'{delivery_expectation,schedule,enabled}' = 'true'
                  AND config#>>'{delivery_expectation,schedule,slot_id}'
                        = 'western_markets_window'
                  AND config#>>'{delivery_expectation,schedule,local_time}' = '10:30:00'
                  AND config#>>'{delivery_expectation,schedule,timezone}' = 'Asia/Taipei'
                  AND config#>'{delivery_expectation,schedule,expected_sources}'
                        = '["twelve_data"]'::jsonb
                  AND config#>>'{delivery_expectation,schedule,target_date_lag_days}' = '1'
                  AND (
                      SELECT count(*)
                      FROM scheduler_dataset
                      WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                  ) = 1
                  AND EXISTS (
                      SELECT 1
                      FROM scheduler_dataset
                      WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        AND dataset_key = 'us_equity_eod'
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM scheduler_control
                      WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        AND provider = 'twelve_data'
                        AND slot_id = 'western_markets_window'
                        AND scheduled_local_time = TIME '08:15:00'
                        AND timezone = 'Asia/Taipei'
                        AND dataset_keys = '["us_equity_eod"]'::jsonb
                  );
                GET DIAGNOSTICS policy_rows = ROW_COUNT;

                IF policy_rows <> 1 THEN
                    RAISE EXCEPTION
                        'Twelve Data delivery policy update did not affect exactly one row';
                END IF;
            END $$
            """
        )
    )


def downgrade() -> None:
    """Revert only an untouched copy of the governed post-upgrade values."""

    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                scheduler_rows integer;
                policy_rows integer;
            BEGIN
                -- Require the complete post-upgrade pair before changing
                -- either side.  A one-sided operator drift is a safe no-op.
                UPDATE scheduler_control
                SET scheduled_local_time = TIME '10:30:00',
                    updated_at = now()
                WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                  AND provider = 'twelve_data'
                  AND slot_id = 'western_markets_window'
                  AND scheduled_local_time = TIME '08:15:00'
                  AND timezone = 'Asia/Taipei'
                  AND dataset_keys = '["us_equity_eod"]'::jsonb
                  AND (
                      SELECT count(*)
                      FROM scheduler_dataset
                      WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                  ) = 1
                  AND EXISTS (
                      SELECT 1
                      FROM scheduler_dataset
                      WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        AND dataset_key = 'us_equity_eod'
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM dataset_registry
                      WHERE dataset_key = 'us_equity_eod'
                        AND jsonb_typeof(config) = 'object'
                        AND jsonb_typeof(config->'delivery_expectation') = 'object'
                        AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
                        AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                              = '["twelve_data"]'::jsonb
                        AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                              = '09:15:00'
                        AND jsonb_typeof(config#>'{delivery_expectation,schedule}') = 'object'
                        AND config#>>'{delivery_expectation,schedule,enabled}' = 'true'
                        AND config#>>'{delivery_expectation,schedule,slot_id}'
                              = 'western_markets_window'
                        AND config#>>'{delivery_expectation,schedule,local_time}' = '08:15:00'
                        AND config#>>'{delivery_expectation,schedule,timezone}' = 'Asia/Taipei'
                        AND config#>'{delivery_expectation,schedule,expected_sources}'
                              = '["twelve_data"]'::jsonb
                        AND config#>>'{delivery_expectation,schedule,target_date_lag_days}' = '1'
                  );
                GET DIAGNOSTICS scheduler_rows = ROW_COUNT;

                IF scheduler_rows = 0 THEN
                    RETURN;
                END IF;
                IF scheduler_rows <> 1 THEN
                    RAISE EXCEPTION
                        'Twelve Data scheduler downgrade did not affect exactly one row';
                END IF;

                UPDATE dataset_registry
                SET config = jsonb_set(
                        jsonb_set(
                            config,
                            '{delivery_expectation,schedule,local_time}',
                            '"10:30:00"'::jsonb,
                            true
                        ),
                        '{delivery_expectation,missing_delivery,deadline_local_time}',
                        '"11:30:00"'::jsonb,
                        true
                    ),
                    updated_at = now()
                WHERE dataset_key = 'us_equity_eod'
                  AND jsonb_typeof(config) = 'object'
                  AND jsonb_typeof(config->'delivery_expectation') = 'object'
                  AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
                  AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                        = '["twelve_data"]'::jsonb
                  AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                        = '09:15:00'
                  AND jsonb_typeof(config#>'{delivery_expectation,schedule}') = 'object'
                  AND config#>>'{delivery_expectation,schedule,enabled}' = 'true'
                  AND config#>>'{delivery_expectation,schedule,slot_id}'
                        = 'western_markets_window'
                  AND config#>>'{delivery_expectation,schedule,local_time}' = '08:15:00'
                  AND config#>>'{delivery_expectation,schedule,timezone}' = 'Asia/Taipei'
                  AND config#>'{delivery_expectation,schedule,expected_sources}'
                        = '["twelve_data"]'::jsonb
                  AND config#>>'{delivery_expectation,schedule,target_date_lag_days}' = '1'
                  AND (
                      SELECT count(*)
                      FROM scheduler_dataset
                      WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                  ) = 1
                  AND EXISTS (
                      SELECT 1
                      FROM scheduler_dataset
                      WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        AND dataset_key = 'us_equity_eod'
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM scheduler_control
                      WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                        AND provider = 'twelve_data'
                        AND slot_id = 'western_markets_window'
                        AND scheduled_local_time = TIME '10:30:00'
                        AND timezone = 'Asia/Taipei'
                        AND dataset_keys = '["us_equity_eod"]'::jsonb
                  );
                GET DIAGNOSTICS policy_rows = ROW_COUNT;

                IF policy_rows <> 1 THEN
                    RAISE EXCEPTION
                        'Twelve Data delivery policy downgrade did not affect exactly one row';
                END IF;
            END $$
            """
        )
    )
