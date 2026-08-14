"""Move the governed Twelve Data run to 09:00 Taipei.

Revision ID: f2a3b4c5d6e7
Revises: f1a2b3c4d5e6

The scheduler definition and dataset delivery policy must change together.
Upgrade therefore validates the known Twelve Data control/mapping/config
before changing governed fields.  Unrelated operator JSON is preserved.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCHEDULER_KEY = "twelve_data_us_common_stocks_daily_v1"
_DATASET_KEY = "us_equity_eod"


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
                      AND scheduled_local_time = TIME '08:15:00'
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
            UPDATE scheduler_control
            SET scheduled_local_time = TIME '09:00:00',
                updated_at = now()
            WHERE scheduler_key = :scheduler_key
              AND provider = 'twelve_data'
              AND slot_id = 'western_markets_window'
              AND scheduled_local_time = TIME '08:15:00'
              AND timezone = 'Asia/Taipei'
              AND dataset_keys = '["us_equity_eod"]'::jsonb
            """
        ).bindparams(scheduler_key=_SCHEDULER_KEY)
    )
    op.execute(
        sa.text(
            """
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
            WHERE dataset_key = :dataset_key
              AND config#>>'{delivery_expectation,schedule,slot_id}'
                    = 'western_markets_window'
              AND config#>>'{delivery_expectation,schedule,local_time}' = '08:15:00'
              AND config#>>'{delivery_expectation,schedule,timezone}' = 'Asia/Taipei'
              AND config#>'{delivery_expectation,schedule,expected_sources}'
                    = '["twelve_data"]'::jsonb
              AND config#>>'{delivery_expectation,schedule,target_date_lag_days}' = '1'
              AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
              AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                    = '["twelve_data"]'::jsonb
              AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                    = '09:15:00'
            """
        ).bindparams(dataset_key=_DATASET_KEY)
    )


def downgrade() -> None:
    """Revert only an untouched copy of the governed post-upgrade values."""

    op.execute(
        sa.text(
            """
            UPDATE scheduler_control
            SET scheduled_local_time = TIME '08:15:00',
                updated_at = now()
            WHERE scheduler_key = :scheduler_key
              AND provider = 'twelve_data'
              AND slot_id = 'western_markets_window'
              AND scheduled_local_time = TIME '09:00:00'
              AND timezone = 'Asia/Taipei'
              AND dataset_keys = '["us_equity_eod"]'::jsonb
              AND EXISTS (
                  SELECT 1
                  FROM dataset_registry
                  WHERE dataset_key = 'us_equity_eod'
                    AND config#>>'{delivery_expectation,schedule,slot_id}'
                          = 'western_markets_window'
                    AND config#>>'{delivery_expectation,schedule,local_time}' = '09:00:00'
                    AND config#>>'{delivery_expectation,schedule,timezone}' = 'Asia/Taipei'
                    AND config#>'{delivery_expectation,schedule,expected_sources}'
                          = '["twelve_data"]'::jsonb
                    AND config#>>'{delivery_expectation,schedule,target_date_lag_days}' = '1'
                    AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
                    AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                          = '["twelve_data"]'::jsonb
                    AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                          = '10:00:00'
              )
            """
        ).bindparams(scheduler_key=_SCHEDULER_KEY)
    )
    op.execute(
        sa.text(
            """
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
            WHERE dataset_key = :dataset_key
              AND config#>>'{delivery_expectation,schedule,slot_id}'
                    = 'western_markets_window'
              AND config#>>'{delivery_expectation,schedule,local_time}' = '09:00:00'
              AND config#>>'{delivery_expectation,schedule,timezone}' = 'Asia/Taipei'
              AND config#>'{delivery_expectation,schedule,expected_sources}'
                    = '["twelve_data"]'::jsonb
              AND config#>>'{delivery_expectation,schedule,target_date_lag_days}' = '1'
              AND config#>>'{delivery_expectation,missing_delivery,action}' = 'warn'
              AND config#>'{delivery_expectation,missing_delivery,expected_sources}'
                    = '["twelve_data"]'::jsonb
              AND config#>>'{delivery_expectation,missing_delivery,deadline_local_time}'
                    = '10:00:00'
              AND EXISTS (
                  SELECT 1
                  FROM scheduler_control
                  WHERE scheduler_key = 'twelve_data_us_common_stocks_daily_v1'
                    AND provider = 'twelve_data'
                    AND slot_id = 'western_markets_window'
                    AND scheduled_local_time = TIME '08:15:00'
                    AND timezone = 'Asia/Taipei'
                    AND dataset_keys = '["us_equity_eod"]'::jsonb
              )
            """
        ).bindparams(dataset_key=_DATASET_KEY)
    )
