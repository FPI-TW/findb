"""Migrate scheduler slots to semantic, time-independent identities.

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d8e9

The data rewrite is intentionally explicit.  Slot IDs are canonicalized once
in PostgreSQL while their authoritative local times are carried as independent
metadata values.  The migration also renames the one legacy FinLab control key
without changing any mutable control-plane state or historical audit rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGACY_FINLAB_KEY = "finlab_tw_1430_tw_equity_eod"
_CANONICAL_FINLAB_KEY = "finlab_tw_equity_eod_v1"
_LEGACY_SLOT_IDS = ("us_0600", "global_0815", "tw_1430", "asia_1630")
_CANONICAL_SLOT_IDS = (
    "western_markets_window",
    "global_markets_window",
    "taiwan_market_window",
    "asia_pacific_markets_window",
)
_ALL_SLOT_IDS = _LEGACY_SLOT_IDS + _CANONICAL_SLOT_IDS


def _slot_list_sql(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _validate_slot_values(*, allowed: tuple[str, ...]) -> None:
    """Fail closed before any governed row can be partially rewritten."""

    allowed_sql = _slot_list_sql(allowed)
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM scheduler_control
                    WHERE slot_id IS NULL OR slot_id NOT IN ({allowed_sql})
                ) THEN
                    RAISE EXCEPTION
                        'unsupported scheduler_control.slot_id; migration aborted';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM dataset_registry
                    WHERE jsonb_typeof(config->'delivery_expectation'->'schedule') = 'object'
                      AND config->'delivery_expectation'->'schedule' ? 'slot_id'
                      AND COALESCE(
                            config->'delivery_expectation'->'schedule'->>'slot_id', ''
                          ) NOT IN ({allowed_sql})
                ) THEN
                    RAISE EXCEPTION
                        'unsupported delivery_expectation.schedule.slot_id; migration aborted';
                END IF;
            END
            $$;
            """
        )
    )


def _assert_slot_values(*, allowed: tuple[str, ...], label: str) -> None:
    """Assert that governed rows contain no legacy or unknown IDs."""

    allowed_sql = _slot_list_sql(allowed)
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM scheduler_control
                    WHERE slot_id IS NULL OR slot_id NOT IN ({allowed_sql})
                ) OR EXISTS (
                    SELECT 1
                    FROM dataset_registry
                    WHERE jsonb_typeof(config->'delivery_expectation'->'schedule') = 'object'
                      AND config->'delivery_expectation'->'schedule' ? 'slot_id'
                      AND COALESCE(
                            config->'delivery_expectation'->'schedule'->>'slot_id', ''
                          ) NOT IN ({allowed_sql})
                ) THEN
                    RAISE EXCEPTION '{label}; migration aborted';
                END IF;
            END
            $$;
            """
        )
    )


def _rename_finlab_scheduler(*, old_key: str, new_key: str) -> None:
    """Rename one control row and its association rows without state loss."""

    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM scheduler_control WHERE scheduler_key = '{old_key}'
                ) AND EXISTS (
                    SELECT 1 FROM scheduler_control WHERE scheduler_key = '{new_key}'
                ) THEN
                    RAISE EXCEPTION
                        'scheduler key collision: both {old_key} and {new_key} exist';
                END IF;
            END
            $$;
            """
        )
    )

    # scheduler_dataset's historical FK was intentionally not ON UPDATE
    # CASCADE.  Temporarily dropping it lets us update the parent primary key
    # and the child mappings in one transaction, then re-establishes the same
    # restrictive relationship.
    op.execute(
        sa.text(
            "ALTER TABLE scheduler_dataset "
            "DROP CONSTRAINT IF EXISTS fk_scheduler_dataset_scheduler_key"
        )
    )
    op.execute(
        sa.text(
            "UPDATE scheduler_control SET scheduler_key = :new_key WHERE scheduler_key = :old_key"
        ).bindparams(old_key=old_key, new_key=new_key)
    )
    op.execute(
        sa.text(
            "UPDATE scheduler_dataset SET scheduler_key = :new_key WHERE scheduler_key = :old_key"
        ).bindparams(old_key=old_key, new_key=new_key)
    )
    op.execute(
        sa.text(
            "ALTER TABLE scheduler_dataset ADD CONSTRAINT "
            "fk_scheduler_dataset_scheduler_key FOREIGN KEY (scheduler_key) "
            "REFERENCES scheduler_control (scheduler_key) ON DELETE CASCADE"
        )
    )


def _canonicalize_slots() -> None:
    # This update deliberately keeps local_time independent of the Python
    # vocabulary.  The four values are the one-time canonical timing metadata
    # for this migration's rollout.
    op.execute(
        sa.text(
            """
            UPDATE scheduler_control
            SET slot_id = CASE slot_id
                    WHEN 'us_0600' THEN 'western_markets_window'
                    WHEN 'global_0815' THEN 'global_markets_window'
                    WHEN 'tw_1430' THEN 'taiwan_market_window'
                    WHEN 'asia_1630' THEN 'asia_pacific_markets_window'
                    ELSE slot_id
                END,
                scheduled_local_time = CASE slot_id
                    WHEN 'us_0600' THEN CASE
                        WHEN scheduled_local_time = TIME '06:00:00' THEN TIME '06:30:00'
                        ELSE scheduled_local_time
                    END
                    WHEN 'asia_1630' THEN CASE
                        WHEN scheduled_local_time = TIME '16:30:00' THEN TIME '17:15:00'
                        ELSE scheduled_local_time
                    END
                    ELSE scheduled_local_time
                END
            WHERE slot_id IN ('us_0600', 'global_0815', 'tw_1430', 'asia_1630')
            """
        )
    )

    op.execute(
        sa.text(
            """
            UPDATE dataset_registry
            SET config = jsonb_set(
                    CASE config->'delivery_expectation'->'schedule'->>'slot_id'
                        WHEN 'us_0600' THEN CASE
                            WHEN config->'delivery_expectation'->'schedule'->>'local_time'
                                    = '06:00:00'
                            THEN jsonb_set(
                                config,
                                '{delivery_expectation,schedule,local_time}',
                                '"06:30:00"'::jsonb,
                                true
                            )
                            ELSE config
                        END
                        WHEN 'asia_1630' THEN CASE
                            WHEN config->'delivery_expectation'->'schedule'->>'local_time'
                                    = '16:30:00'
                            THEN jsonb_set(
                                config,
                                '{delivery_expectation,schedule,local_time}',
                                '"17:15:00"'::jsonb,
                                true
                            )
                            ELSE config
                        END
                        ELSE config
                    END,
                    '{delivery_expectation,schedule,slot_id}',
                    to_jsonb(
                        CASE config->'delivery_expectation'->'schedule'->>'slot_id'
                            WHEN 'us_0600' THEN 'western_markets_window'
                            WHEN 'global_0815' THEN 'global_markets_window'
                            WHEN 'tw_1430' THEN 'taiwan_market_window'
                            WHEN 'asia_1630' THEN 'asia_pacific_markets_window'
                        END
                    ),
                    true
                ),
                updated_at = now()
            WHERE jsonb_typeof(config->'delivery_expectation'->'schedule') = 'object'
              AND config->'delivery_expectation'->'schedule'->>'slot_id'
                    IN ('us_0600', 'global_0815', 'tw_1430', 'asia_1630')
            """
        )
    )


def _legacy_slots() -> None:
    """Restore the pre-migration IDs/times for an explicit downgrade."""

    op.execute(
        sa.text(
            """
            UPDATE scheduler_control
            SET slot_id = CASE slot_id
                    WHEN 'western_markets_window' THEN 'us_0600'
                    WHEN 'global_markets_window' THEN 'global_0815'
                    WHEN 'taiwan_market_window' THEN 'tw_1430'
                    WHEN 'asia_pacific_markets_window' THEN 'asia_1630'
                    ELSE slot_id
                END,
                scheduled_local_time = CASE slot_id
                    WHEN 'western_markets_window' THEN CASE
                        WHEN scheduled_local_time = TIME '06:30:00' THEN TIME '06:00:00'
                        ELSE scheduled_local_time
                    END
                    WHEN 'asia_pacific_markets_window' THEN CASE
                        WHEN scheduled_local_time = TIME '17:15:00' THEN TIME '16:30:00'
                        ELSE scheduled_local_time
                    END
                    ELSE scheduled_local_time
                END
            WHERE slot_id IN (
                'western_markets_window',
                'global_markets_window',
                'taiwan_market_window',
                'asia_pacific_markets_window'
            )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE dataset_registry
            SET config = jsonb_set(
                    CASE config->'delivery_expectation'->'schedule'->>'slot_id'
                        WHEN 'western_markets_window' THEN CASE
                            WHEN config->'delivery_expectation'->'schedule'->>'local_time'
                                    = '06:30:00'
                            THEN jsonb_set(
                                config,
                                '{delivery_expectation,schedule,local_time}',
                                '"06:00:00"'::jsonb,
                                true
                            )
                            ELSE config
                        END
                        WHEN 'asia_pacific_markets_window' THEN CASE
                            WHEN config->'delivery_expectation'->'schedule'->>'local_time'
                                    = '17:15:00'
                            THEN jsonb_set(
                                config,
                                '{delivery_expectation,schedule,local_time}',
                                '"16:30:00"'::jsonb,
                                true
                            )
                            ELSE config
                        END
                        ELSE config
                    END,
                    '{delivery_expectation,schedule,slot_id}',
                    to_jsonb(
                        CASE config->'delivery_expectation'->'schedule'->>'slot_id'
                            WHEN 'western_markets_window' THEN 'us_0600'
                            WHEN 'global_markets_window' THEN 'global_0815'
                            WHEN 'taiwan_market_window' THEN 'tw_1430'
                            WHEN 'asia_pacific_markets_window' THEN 'asia_1630'
                        END
                    ),
                    true
                ),
                updated_at = now()
            WHERE jsonb_typeof(config->'delivery_expectation'->'schedule') = 'object'
              AND config->'delivery_expectation'->'schedule'->>'slot_id'
                    IN (
                        'western_markets_window',
                        'global_markets_window',
                        'taiwan_market_window',
                        'asia_pacific_markets_window'
                    )
            """
        )
    )


def upgrade() -> None:
    _validate_slot_values(allowed=_ALL_SLOT_IDS)
    _canonicalize_slots()
    _assert_slot_values(allowed=_CANONICAL_SLOT_IDS, label="non-canonical slot value")
    _rename_finlab_scheduler(old_key=_LEGACY_FINLAB_KEY, new_key=_CANONICAL_FINLAB_KEY)


def downgrade() -> None:
    _validate_slot_values(allowed=_ALL_SLOT_IDS)
    _legacy_slots()
    _assert_slot_values(allowed=_LEGACY_SLOT_IDS, label="non-legacy slot value")
    _rename_finlab_scheduler(old_key=_CANONICAL_FINLAB_KEY, new_key=_LEGACY_FINLAB_KEY)
