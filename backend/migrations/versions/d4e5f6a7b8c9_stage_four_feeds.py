"""Converge staging registry/auth scopes on the four versioned feeds.

The upgrade is intentionally fail-closed for *obsolete dataset data*: legacy
raw/workflow/alert rows and registry foreign-key rows must be absent before
their dataset registry entries are removed.  Canonical tables and rows for the
four surviving feeds are not touched.  The three supported scheduler controls
are retained; unsupported scheduler definitions are removed after their scope
associations are cleared.  Source credentials are preserved.  Provider-wide
NULL scopes for the three supported providers are narrowed to their exact feed
mapping; existing non-null allowlists are intersected with that mapping.
Unsupported providers become deny-all without deleting their audit identity.

Downgrade is lossy.  It removes the new contract declaration keys from the four
surviving rows but cannot recreate deleted legacy registry rows or their
operator configuration.  Historical Alembic revisions remain untouched.
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DATASETS = ("us_equity_eod", "tw_equity_eod", "tw_equity_minute", "tw_etf_minute")


def _assert_no_obsolete_dataset_rows() -> None:
    """Abort before registry deletion when rows still reference old feeds.

    Supported-dataset rows are deliberately allowed to remain.  The cutover
    only removes unsupported registry keys, so blocking on canonical tables or
    on the complete staging database would make a safe, already-populated
    four-feed environment impossible to upgrade.
    """
    dataset_tables = (
        "raw.market_payload",
        "ingestion_attempt",
        "ingestion_run",
        "normalization_job",
        "missing_delivery_alert",
        "tw_minute_archive_release",
        "tw_minute_dataset_snapshot",
        "tw_minute_universe_release",
    )
    bind = op.get_bind()
    for qualified_table in dataset_tables:
        # ``ingestion_attempt.dataset_key`` is nullable for rejected or
        # unauthenticated audit attempts.  A NULL row has no dataset scope and
        # must not block a feed cutover; all other workflow tables are strict.
        dataset_predicate = (
            "dataset_key IS NOT NULL AND dataset_key NOT IN :dataset_keys"
            if qualified_table == "ingestion_attempt"
            else "dataset_key IS NULL OR dataset_key NOT IN :dataset_keys"
        )
        count = bind.execute(
            sa.text(f"SELECT count(*) FROM {qualified_table} WHERE {dataset_predicate}").bindparams(
                sa.bindparam("dataset_keys", expanding=True)
            ),
            {"dataset_keys": _DATASETS},
        ).scalar_one()
        if int(count) != 0:
            raise RuntimeError(
                "staging feed cutover refused: obsolete dataset rows remain "
                f"({qualified_table}={count}); stop writers and clear legacy data first"
            )

    # Outbox/DQ rows inherit dataset identity through their ingestion run.
    for qualified_table, alias in (("normalization_outbox", "o"), ("dq_issue", "d")):
        count = bind.execute(
            sa.text(
                f"SELECT count(*) FROM {qualified_table} AS {alias} "
                f"JOIN ingestion_run AS r ON r.run_id = {alias}.run_id "
                "WHERE r.dataset_key NOT IN :dataset_keys"
            ).bindparams(sa.bindparam("dataset_keys", expanding=True)),
            {"dataset_keys": _DATASETS},
        ).scalar_one()
        if int(count) != 0:
            raise RuntimeError(
                "staging feed cutover refused: obsolete dataset rows remain "
                f"({qualified_table}={count}); stop writers and clear legacy data first"
            )


def _update_contract_configs() -> None:
    bind = op.get_bind()
    configs = {
        "us_equity_eod": {
            "schema_id": "market_eod",
            "defaults": {"market": "US", "asset_class": "equity", "currency": "USD"},
            "allowed_sources": ["twelve_data"],
        },
        "tw_equity_eod": {
            "schema_id": "market_eod",
            "defaults": {"market": "TW", "asset_class": "equity", "currency": "TWD"},
            "allowed_sources": ["finlab"],
        },
        "tw_equity_minute": {
            "schema_id": "market_minute",
            "defaults": {
                "market": "TW",
                "asset_class": "equity",
                "currency": "TWD",
                "market_timezone": "Asia/Taipei",
                "price_adjustment": "none",
            },
            "allowed_sources": ["shioaji"],
        },
        "tw_etf_minute": {
            "schema_id": "market_minute",
            "defaults": {
                "market": "TW",
                "asset_class": "etf",
                "currency": "TWD",
                "market_timezone": "Asia/Taipei",
                "price_adjustment": "none",
            },
            "allowed_sources": ["shioaji"],
        },
    }
    for dataset_key, declaration in configs.items():
        bind.execute(
            sa.text(
                """
                UPDATE dataset_registry
                SET config = (
                    (coalesce(config, '{}'::jsonb) - ARRAY[
                        'source_format','field_mapping','data_path','symbol_field',
                        'name_field','source_field','identifier_field','identifier_type'
                    ]::text[])
                    || jsonb_build_object(
                        'schema_id', CAST(:schema_id AS text),
                        'accepted_schema_versions', '[1]'::jsonb,
                        'current_schema_version', 1,
                        'schema_enforcement', 'enforce',
                        'allowed_sources', CAST(:allowed_sources AS jsonb),
                        'defaults', CAST(:defaults AS jsonb),
                        'delivery_expectation',
                            coalesce(config->'delivery_expectation', '{}'::jsonb)
                            || CASE
                                WHEN config#>'{delivery_expectation,missing_delivery}' IS NULL
                                  OR (
                                      :dataset_key = 'tw_equity_eod'
                                      AND config#>'{delivery_expectation,missing_delivery}'
                                          = '{"action":"disabled","expected_sources":[]}'::jsonb
                                  )
                                THEN jsonb_build_object(
                                    'missing_delivery', CAST(:missing_delivery AS jsonb)
                                )
                                ELSE '{}'::jsonb
                            END
                    )
                ),
                    is_active = true,
                    updated_at = now()
                WHERE dataset_key = :dataset_key
                """
            ),
            {
                "dataset_key": dataset_key,
                "schema_id": declaration["schema_id"],
                "allowed_sources": json.dumps(declaration["allowed_sources"]),
                "defaults": json.dumps(declaration["defaults"]),
                "missing_delivery": json.dumps(
                    {
                        "action": "warn",
                        "expected_sources": declaration["allowed_sources"],
                        "deadline_local_time": "17:00:00",
                    }
                ),
            },
        )


def upgrade() -> None:
    _assert_no_obsolete_dataset_rows()
    bind = op.get_bind()

    # Normalize the association table to the exact three scheduler/four-feed
    # scope.  The association table is authoritative over the JSON projection.
    bind.execute(
        sa.text(
            """
            DELETE FROM scheduler_dataset
            WHERE (scheduler_key, dataset_key) NOT IN (
                ('twelve_data_us_common_stocks_daily_v1', 'us_equity_eod'),
                ('finlab_tw_equity_eod_v1', 'tw_equity_eod'),
                ('shioaji_tw_pilot_v1', 'tw_equity_minute'),
                ('shioaji_tw_pilot_v1', 'tw_etf_minute')
            )
            """
        )
    )
    # SchedulerControl.dataset_keys has a historical non-empty-array check.
    # Unsupported controls cannot be safely stopped by assigning ``[]``;
    # remove the definition after its association rows have been removed.
    bind.execute(
        sa.text(
            """
            DELETE FROM scheduler_control
            WHERE scheduler_key NOT IN (
                'twelve_data_us_common_stocks_daily_v1',
                'finlab_tw_equity_eod_v1',
                'shioaji_tw_pilot_v1'
            )
            """
        )
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO scheduler_dataset (scheduler_key, dataset_key)
            SELECT expected.scheduler_key, expected.dataset_key
            FROM (VALUES
                ('twelve_data_us_common_stocks_daily_v1', 'us_equity_eod'),
                ('finlab_tw_equity_eod_v1', 'tw_equity_eod'),
                ('shioaji_tw_pilot_v1', 'tw_equity_minute'),
                ('shioaji_tw_pilot_v1', 'tw_etf_minute')
            ) AS expected(scheduler_key, dataset_key)
            JOIN scheduler_control AS control
              ON control.scheduler_key = expected.scheduler_key
            ON CONFLICT (scheduler_key, dataset_key) DO NOTHING
            """
        )
    )
    # Keep the compatibility JSON projection bounded to supported keys.  The
    # unsupported scheduler definitions were removed above because the schema
    # requires a non-empty dataset array and must not be widened or fabricated.
    bind.execute(
        sa.text(
            """
            UPDATE scheduler_control AS sc
            SET dataset_keys = CASE sc.scheduler_key
                WHEN 'twelve_data_us_common_stocks_daily_v1' THEN '["us_equity_eod"]'::jsonb
                WHEN 'finlab_tw_equity_eod_v1' THEN '["tw_equity_eod"]'::jsonb
                WHEN 'shioaji_tw_pilot_v1' THEN '["tw_equity_minute","tw_etf_minute"]'::jsonb
                ELSE '[]'::jsonb
            END,
            desired_state = CASE
                WHEN sc.scheduler_key IN (
                    'twelve_data_us_common_stocks_daily_v1',
                    'finlab_tw_equity_eod_v1',
                    'shioaji_tw_pilot_v1'
                ) THEN sc.desired_state
                ELSE 'stopped'
            END,
            observed_state = CASE
                WHEN sc.scheduler_key IN (
                    'twelve_data_us_common_stocks_daily_v1',
                    'finlab_tw_equity_eod_v1',
                    'shioaji_tw_pilot_v1'
                ) THEN sc.observed_state
                ELSE 'stopped'
            END,
            updated_at = now()
            WHERE sc.dataset_keys IS DISTINCT FROM CASE sc.scheduler_key
                    WHEN 'twelve_data_us_common_stocks_daily_v1' THEN '["us_equity_eod"]'::jsonb
                    WHEN 'finlab_tw_equity_eod_v1' THEN '["tw_equity_eod"]'::jsonb
                    WHEN 'shioaji_tw_pilot_v1' THEN '["tw_equity_minute","tw_etf_minute"]'::jsonb
                    ELSE '[]'::jsonb
                END
               OR (
                    sc.scheduler_key NOT IN (
                        'twelve_data_us_common_stocks_daily_v1',
                        'finlab_tw_equity_eod_v1',
                        'shioaji_tw_pilot_v1'
                    )
                    AND (sc.desired_state <> 'stopped' OR sc.observed_state <> 'stopped')
               )
            """
        )
    )

    # Scope credentials without deleting/revoking their audit identity.
    # Provider-wide NULL scopes become the exact supported provider mapping;
    # malformed or unsupported scopes become [] (deny-all).
    bind.execute(
        sa.text(
            """
            WITH scoped AS (
                SELECT client_id,
                       lower(trim(source_name)) AS normalized_source,
                       CASE
                    WHEN lower(trim(source_name)) = 'twelve_data'
                         AND allowed_datasets IS NULL THEN '["us_equity_eod"]'::jsonb
                    WHEN lower(trim(source_name)) = 'twelve_data'
                         AND jsonb_typeof(allowed_datasets) = 'array' THEN
                        (SELECT coalesce(jsonb_agg(value), '[]'::jsonb)
                           FROM jsonb_array_elements_text(allowed_datasets) AS values(value)
                          WHERE value = 'us_equity_eod')
                    WHEN lower(trim(source_name)) = 'finlab'
                         AND allowed_datasets IS NULL THEN '["tw_equity_eod"]'::jsonb
                    WHEN lower(trim(source_name)) = 'finlab'
                         AND jsonb_typeof(allowed_datasets) = 'array' THEN
                        (SELECT coalesce(jsonb_agg(value), '[]'::jsonb)
                           FROM jsonb_array_elements_text(allowed_datasets) AS values(value)
                          WHERE value = 'tw_equity_eod')
                    WHEN lower(trim(source_name)) = 'shioaji'
                         AND allowed_datasets IS NULL THEN
                        '["tw_equity_minute","tw_etf_minute"]'::jsonb
                    WHEN lower(trim(source_name)) = 'shioaji'
                         AND jsonb_typeof(allowed_datasets) = 'array' THEN
                        (SELECT coalesce(jsonb_agg(value), '[]'::jsonb)
                           FROM jsonb_array_elements_text(allowed_datasets) AS values(value)
                          WHERE value IN ('tw_equity_minute','tw_etf_minute'))
                    ELSE '[]'::jsonb
                       END AS desired_scope
                FROM source_client
            )
            UPDATE source_client AS client
            SET source_name = scoped.normalized_source,
                allowed_datasets = scoped.desired_scope,
                updated_at = now()
            FROM scoped
            WHERE client.client_id = scoped.client_id
              AND (
                  client.source_name IS DISTINCT FROM scoped.normalized_source
                  OR client.allowed_datasets IS DISTINCT FROM scoped.desired_scope
              )
            """
        )
    )

    _update_contract_configs()
    bind.execute(
        sa.text("DELETE FROM dataset_registry WHERE dataset_key NOT IN :dataset_keys").bindparams(
            sa.bindparam("dataset_keys", expanding=True)
        ),
        {"dataset_keys": _DATASETS},
    )


def downgrade() -> None:
    """Lossy rollback: remove contract-only declaration keys from survivors."""
    op.execute(
        sa.text(
            """
            UPDATE dataset_registry
            SET config = coalesce(config, '{}'::jsonb)
                - 'schema_id'
                - 'accepted_schema_versions'
                - 'current_schema_version'
                - 'schema_enforcement'
                - 'allowed_sources'
                - 'defaults',
                updated_at = now()
            WHERE dataset_key IN :dataset_keys
            """
        ).bindparams(sa.bindparam("dataset_keys", expanding=True, value=_DATASETS))
    )
