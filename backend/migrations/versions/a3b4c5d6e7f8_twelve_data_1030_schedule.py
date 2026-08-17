"""Move the governed Twelve Data run to 10:30 Taipei.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7

The scheduler definition and dataset delivery policy must change together.
Upgrade therefore validates the known Twelve Data control/mapping/config
before changing governed fields.  Unrelated operator JSON is preserved.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _validate_upgrade_shape() -> None:
    """Abort before mutation when governed Twelve Data state has drifted."""

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
                      AND scheduled_local_time = TIME '09:00:00'
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
                            = '10:00:00'
                      AND jsonb_typeof(config#>'{delivery_expectation,schedule}') = 'object'
                      AND config#>>'{delivery_expectation,schedule,enabled}' = 'true'
                      AND config#>>'{delivery_expectation,schedule,slot_id}'
                            = 'western_markets_window'
                      AND config#>>'{delivery_expectation,schedule,local_time}' = '09:00:00'
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
                -- Guard the first mutation with the complete prior governed
                -- pair.  The normalized mapping is part of scheduler identity.
                UPDATE scheduler_control
                SET scheduled_local_time = TIME '10:30:00',
                    updated_at = now()
                WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                  AND provider = 'twelve_data'
                  AND slot_id = 'western_markets_window'
                  AND scheduled_local_time = TIME '09:00:00'
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
                        AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
                        AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                              = '["twelve_data"]'::jsonb
                        AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                              = '10:00:00'
                        AND config#>>'{delivery_expectation,schedule,enabled}' = 'true'
                        AND config#>>'{delivery_expectation,schedule,slot_id}'
                              = 'western_markets_window'
                        AND config#>>'{delivery_expectation,schedule,local_time}' = '09:00:00'
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

                -- Re-check the complete pair after the first mutation.  Any
                -- intervening drift makes this update fail and rolls back both.
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
                  AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
                  AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                        = '["twelve_data"]'::jsonb
                  AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                        = '10:00:00'
                  AND config#>>'{delivery_expectation,schedule,enabled}' = 'true'
                  AND config#>>'{delivery_expectation,schedule,slot_id}'
                        = 'western_markets_window'
                  AND config#>>'{delivery_expectation,schedule,local_time}' = '09:00:00'
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
                -- Capture the complete governed post-upgrade pair before either
                -- table is changed.  A one-sided operator drift must leave both
                -- rows untouched, even when the drift happens to equal 09:00.
                UPDATE scheduler_control
                SET scheduled_local_time = TIME '09:00:00',
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
                        AND config#>>'{delivery_expectation,schedule,enabled}' = 'true'
                        AND config#>>'{delivery_expectation,schedule,slot_id}'
                              = 'western_markets_window'
                        AND config#>>'{delivery_expectation,schedule,local_time}' = '10:30:00'
                        AND config#>>'{delivery_expectation,schedule,timezone}' = 'Asia/Taipei'
                        AND config#>'{delivery_expectation,schedule,expected_sources}'
                              = '["twelve_data"]'::jsonb
                        AND config#>>'{delivery_expectation,schedule,target_date_lag_days}' = '1'
                        AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
                        AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                              = '["twelve_data"]'::jsonb
                        AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                              = '11:30:00'
                  );
                GET DIAGNOSTICS scheduler_rows = ROW_COUNT;

                IF scheduler_rows = 1 THEN
                    UPDATE dataset_registry
                    SET config = jsonb_set(
                            jsonb_set(
                                config,
                                '{delivery_expectation,schedule,local_time}',
                                '"09:00:00"'::jsonb,
                                true
                            ),
                            '{delivery_expectation,missing_delivery,deadline_local_time}',
                            '"10:00:00"'::jsonb,
                            true
                        ),
                        updated_at = now()
                    WHERE dataset_key = 'us_equity_eod'
                      AND config#>>'{delivery_expectation,schedule,enabled}' = 'true'
                      AND config#>>'{delivery_expectation,schedule,slot_id}'
                            = 'western_markets_window'
                      AND config#>>'{delivery_expectation,schedule,local_time}' = '10:30:00'
                      AND config#>>'{delivery_expectation,schedule,timezone}' = 'Asia/Taipei'
                      AND config#>'{delivery_expectation,schedule,expected_sources}'
                            = '["twelve_data"]'::jsonb
                      AND config#>>'{delivery_expectation,schedule,target_date_lag_days}' = '1'
                      AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
                      AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                            = '["twelve_data"]'::jsonb
                      AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                            = '11:30:00'
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
                            AND scheduled_local_time = TIME '09:00:00'
                            AND timezone = 'Asia/Taipei'
                            AND dataset_keys = '["us_equity_eod"]'::jsonb
                      );
                    GET DIAGNOSTICS policy_rows = ROW_COUNT;

                    IF policy_rows <> 1 THEN
                        RAISE EXCEPTION
                            'Twelve Data delivery policy downgrade did not affect one row';
                    END IF;
                END IF;
            END $$
            """
        )
    )
