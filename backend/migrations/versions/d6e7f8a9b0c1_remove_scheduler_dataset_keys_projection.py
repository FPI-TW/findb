"""Remove the scheduler dataset-key JSON projection.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0

The association table is the only durable source of scheduler dataset scope
after this migration.  Upgrade is intentionally a validation-only phase until
all governed rows are locked and proven safe; it never repairs or infers a
mapping.  Downgrade is retained for local migration round-trips only and
reconstructs the legacy projection from the association table.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, Sequence[str], None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CANONICAL_SLOT_IDS = (
    "western_markets_window",
    "global_markets_window",
    "taiwan_market_window",
    "asia_pacific_markets_window",
)
_LEGACY_FINLAB_KEY = "finlab_tw_1430_tw_equity_eod"
_LEGACY_PROJECTION_CONSTRAINT = "ck_scheduler_control_dataset_keys_array"
_HISTORICAL_PROJECTION_CONSTRAINT = "ck_scheduler_control_ck_scheduler_control_dataset_keys_array"


def _canonical_slot_sql() -> str:
    return ", ".join(f"'{slot_id}'" for slot_id in _CANONICAL_SLOT_IDS)


def _lock_governed_tables() -> None:
    """Prevent concurrent writes while validating and changing the projection."""

    # Keep this order stable with the foreign-key direction.  Access-exclusive
    # locking also prevents a concurrent deployment or schema observer from
    # seeing a half-validated/half-contracted control plane.
    op.execute(
        sa.text(
            "LOCK TABLE scheduler_control, scheduler_dataset, dataset_registry "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )


def _validate_upgrade() -> None:
    """Fail closed before any schema or data mutation is attempted."""

    canonical_slots = _canonical_slot_sql()
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                -- The projection must be an array of strings.  Use an empty
                -- array for the set queries below only after this explicit
                -- shape check, so malformed JSON cannot be silently treated
                -- as an empty scope.
                IF EXISTS (
                    SELECT 1
                    FROM scheduler_control AS control
                    WHERE control.dataset_keys IS NULL
                       OR jsonb_typeof(control.dataset_keys) <> 'array'
                       OR EXISTS (
                            SELECT 1
                            FROM jsonb_array_elements(
                                CASE
                                    WHEN jsonb_typeof(control.dataset_keys) = 'array'
                                    THEN control.dataset_keys
                                    ELSE '[]'::jsonb
                                END
                            ) AS item(value)
                            WHERE jsonb_typeof(item.value) <> 'string'
                       )
                ) THEN
                    RAISE EXCEPTION
                        'scheduler_control.dataset_keys projection is not a JSON string array; migration aborted';
                END IF;

                -- Compare both directions.  EXCEPT compares sets rather than
                -- array order, while the FK/primary key on scheduler_dataset
                -- keeps each association unique.
                IF EXISTS (
                    SELECT 1
                    FROM scheduler_control AS control
                    WHERE EXISTS (
                        SELECT projected.dataset_key
                        FROM jsonb_array_elements_text(control.dataset_keys)
                            AS projected(dataset_key)
                        EXCEPT
                        SELECT association.dataset_key
                        FROM scheduler_dataset AS association
                        WHERE association.scheduler_key = control.scheduler_key
                    )
                    OR EXISTS (
                        SELECT association.dataset_key
                        FROM scheduler_dataset AS association
                        WHERE association.scheduler_key = control.scheduler_key
                        EXCEPT
                        SELECT projected.dataset_key
                        FROM jsonb_array_elements_text(control.dataset_keys)
                            AS projected(dataset_key)
                    )
                ) THEN
                    RAISE EXCEPTION
                        'scheduler_control.dataset_keys projection does not match scheduler_dataset associations; migration aborted';
                END IF;

                -- A foreign key normally makes this impossible, but keep the
                -- check explicit so a disabled/removed FK cannot hide an
                -- association that has no corresponding control row.
                IF EXISTS (
                    SELECT 1
                    FROM scheduler_dataset AS association
                    LEFT JOIN scheduler_control AS control
                      ON control.scheduler_key = association.scheduler_key
                    WHERE control.scheduler_key IS NULL
                ) THEN
                    RAISE EXCEPTION
                        'scheduler_dataset contains an association without a scheduler_control row; migration aborted';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM scheduler_control
                    WHERE slot_id IS NULL
                       OR slot_id NOT IN ({canonical_slots})
                ) THEN
                    RAISE EXCEPTION
                        'scheduler_control.slot_id is not canonical; migration aborted';
                END IF;

                -- A present delivery schedule is a structured object whose
                -- slot identity must be canonical.  Missing schedule and JSON
                -- null remain equivalent to no configured schedule; any other
                -- malformed shape fails closed rather than being normalized.
                IF EXISTS (
                    SELECT 1
                    FROM dataset_registry
                    WHERE config#>'{{delivery_expectation,schedule}}' IS NOT NULL
                      AND jsonb_typeof(config#>'{{delivery_expectation,schedule}}') <> 'null'
                      AND (
                            jsonb_typeof(config#>'{{delivery_expectation,schedule}}') <> 'object'
                            OR NOT (
                                config#>'{{delivery_expectation,schedule}}' ? 'slot_id'
                            )
                            OR jsonb_typeof(
                                config#>'{{delivery_expectation,schedule}}'->'slot_id'
                            ) <> 'string'
                            OR COALESCE(
                                config#>>'{{delivery_expectation,schedule,slot_id}}', ''
                            ) NOT IN ({canonical_slots})
                      )
                ) THEN
                    RAISE EXCEPTION
                        'dataset delivery schedule is malformed or non-canonical; migration aborted';
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM scheduler_control
                    WHERE scheduler_key = '{_LEGACY_FINLAB_KEY}'
                ) THEN
                    RAISE EXCEPTION
                        'legacy FinLab scheduler key remains; migration aborted';
                END IF;
            END
            $$;
            """
        )
    )


def _drop_projection_constraint() -> None:
    """Drop the logical legacy constraint across historical naming variants."""

    # Historical migrations passed an already ``ck_*``-prefixed name through
    # SQLAlchemy's ``ck_%(table_name)s_%(constraint_name)s`` convention, so
    # databases upgraded from c5 commonly contain the doubled physical name.
    # The direct name is also accepted for databases assembled by a schema
    # tool.  Both variants describe the same old constraint; neither is
    # touched until all validation above has completed.
    op.execute(
        sa.text(
            f"""
            DO $$
            DECLARE
                dropped boolean := false;
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conrelid = 'scheduler_control'::regclass
                      AND conname = '{_LEGACY_PROJECTION_CONSTRAINT}'
                ) THEN
                    EXECUTE 'ALTER TABLE scheduler_control DROP CONSTRAINT '
                            || quote_ident('{_LEGACY_PROJECTION_CONSTRAINT}');
                    dropped := true;
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conrelid = 'scheduler_control'::regclass
                      AND conname = '{_HISTORICAL_PROJECTION_CONSTRAINT}'
                ) THEN
                    EXECUTE 'ALTER TABLE scheduler_control DROP CONSTRAINT '
                            || quote_ident('{_HISTORICAL_PROJECTION_CONSTRAINT}');
                    dropped := true;
                END IF;
                IF NOT dropped THEN
                    RAISE EXCEPTION
                        'scheduler_control.dataset_keys projection constraint is missing; migration aborted';
                END IF;
            END
            $$;
            """
        )
    )


def upgrade() -> None:
    _lock_governed_tables()
    _validate_upgrade()

    # Use literal SQL for the new physical name.  Passing an already-prefixed
    # name to Alembic's naming convention would create a doubled identifier.
    op.execute(
        sa.text(
            "ALTER TABLE scheduler_control ADD CONSTRAINT "
            "ck_scheduler_control_slot_id_canonical CHECK "
            f"(slot_id IN ({_canonical_slot_sql()}))"
        )
    )
    _drop_projection_constraint()
    op.drop_column("scheduler_control", "dataset_keys")


def downgrade() -> None:
    """Restore the projection from associations for local-only round-trips."""

    _lock_governed_tables()
    op.add_column(
        "scheduler_control",
        sa.Column("dataset_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM scheduler_control AS control
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM scheduler_dataset AS association
                        WHERE association.scheduler_key = control.scheduler_key
                    )
                ) THEN
                    RAISE EXCEPTION
                        'cannot restore scheduler_control.dataset_keys without an association; migration aborted';
                END IF;

                -- The scheduler_dataset foreign key normally prevents this
                -- state, but downgrade must remain fail-closed if that FK was
                -- disabled or removed in a local disposable database.  Do
                -- not silently discard an association while reconstructing
                -- the projection.
                IF EXISTS (
                    SELECT 1
                    FROM scheduler_dataset AS association
                    LEFT JOIN scheduler_control AS control
                      ON control.scheduler_key = association.scheduler_key
                    WHERE control.scheduler_key IS NULL
                ) THEN
                    RAISE EXCEPTION
                        'cannot restore scheduler_control.dataset_keys with an orphan scheduler_dataset association; migration aborted';
                END IF;
            END
            $$;
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE scheduler_control AS control
            SET dataset_keys = (
                SELECT jsonb_agg(to_jsonb(association.dataset_key) ORDER BY association.dataset_key)
                FROM scheduler_dataset AS association
                WHERE association.scheduler_key = control.scheduler_key
            )
            """
        )
    )
    op.alter_column("scheduler_control", "dataset_keys", nullable=False)
    op.execute(
        sa.text(
            "ALTER TABLE scheduler_control ADD CONSTRAINT "
            # c5 passed this already-prefixed logical name through the
            # project's ck_<table>_<constraint> naming convention, so this is
            # the exact physical identifier that must be restored.
            f"{_HISTORICAL_PROJECTION_CONSTRAINT} CHECK "
            "(jsonb_typeof(dataset_keys) = 'array' "
            "AND jsonb_array_length(dataset_keys) > 0)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE scheduler_control DROP CONSTRAINT IF EXISTS "
            "ck_scheduler_control_slot_id_canonical"
        )
    )
