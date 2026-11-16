"""Stage the four FinLab intraday feeds: datasets, scheduler scope and credential scope.

Revision ID: f8b0d2c4e6a9
Revises: e7a9c1d3f5b8

The provider catalog now lists four more FinLab acquisition feeds. Three checks
compare exact sets, so the registry, the FinLab scheduler's datasets and the
FinLab Source credentials have to change in the same step as the catalog:

- registry provisioning requires every supported dataset row to exist;
- starting a scheduler requires its datasets to equal the provider's Source scope;
- publishing the schedule requires a credential's datasets to equal the
  provider's acquisition feeds.

The datasets are created inactive and each is gated by its own frozen plan and
published universe, so nothing is acquired until an operator enables it. Only a
credential that already holds FinLab's whole previous scope is widened; a
narrower or revoked one is left alone.

Downgrade removes what this revision added and refuses once any of the
datasets has been used, because their universes, plans, runs and stored
objects cannot be removed by a schema rollback.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "f8b0d2c4e6a9"
down_revision = "e7a9c1d3f5b8"
branch_labels = None
depends_on = None

SCHEDULER_KEY = "full_market_finlab_v1"
PREVIOUS_SCOPE = ["tw_equity_eod", "tw_etf_eod"]
# The catalog order of FinLab's Source scope after this revision.
STAGED_SCOPE = [
    "tw_equity_bar_1m",
    "tw_equity_eod",
    "tw_equity_tick",
    "tw_etf_bar_1m",
    "tw_etf_eod",
    "tw_etf_tick",
]
# dataset: name, asset class, registry frequency, record kind
DATASETS = {
    "tw_equity_bar_1m": ("台股股票 1 分 K", "equity", "minute", "bar_1m"),
    "tw_etf_bar_1m": ("台灣 ETF 1 分 K", "etf", "minute", "bar_1m"),
    "tw_equity_tick": ("台股股票逐筆成交", "equity", "tick", "tick"),
    "tw_etf_tick": ("台灣 ETF 逐筆成交", "etf", "tick", "tick"),
}
DESCRIPTION = (
    "Staged finlab intraday_partition.v1 intraday feed; rows are canonical objects, volume in lots"
)
# Tables that hold rows of a dataset once it has been used.
USAGE_TABLES = (
    "universe_release",
    "daily_delivery_plan",
    "daily_execution_decision",
    "full_market_admission_feed",
    "full_market_dataset_state",
    "ingestion_run",
    "intraday_partition",
    "provider_work",
)


def _config(asset_class: str, record_kind: str) -> dict:
    return {
        "schema_id": "intraday_partition",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "allowed_sources": ["finlab"],
        "defaults": {
            "market": "TW",
            "asset_class": asset_class,
            "currency": "TWD",
            "market_timezone": "Asia/Taipei",
            "price_adjustment": "none",
            "record_kind": record_kind,
        },
        "delivery_expectation": {
            "delivery_mode": "incremental",
            "baseline": {
                "strategy": "rolling_median",
                "scope": "dataset_source_schema",
                "window_size": 7,
                "minimum_history": 3,
                "enabled": False,
            },
            "record_count": {
                "minimum_record_count": 0,
                "maximum_count_drop_ratio": 0.1,
                "action": "disabled",
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
            "missing_delivery": {"action": "disabled", "expected_sources": []},
            "schedule": {
                "enabled": False,
                "slot_id": "taiwan_market_window",
                "local_time": "18:30:00",
                "timezone": "Asia/Taipei",
                "target_date_lag_days": 0,
                "expected_sources": ["finlab"],
            },
        },
        "full_market": {
            "enabled": False,
            "required": True,
            "activation_date": None,
            "calendar_market": "TW",
        },
        "source_precedence": ["finlab"],
    }


def upgrade() -> None:
    bind = op.get_bind()
    for key, (name, asset_class, frequency, record_kind) in DATASETS.items():
        bind.execute(
            sa.text(
                "INSERT INTO dataset_registry (dataset_key, name, description, asset_class, "
                "market, frequency, is_active, config, created_at, updated_at) "
                "VALUES (:key, :name, :description, :asset_class, 'TW', :frequency, false, "
                "CAST(:config AS jsonb), now(), now()) ON CONFLICT (dataset_key) DO NOTHING"
            ),
            {
                "key": key,
                "name": name,
                "description": DESCRIPTION,
                "asset_class": asset_class,
                "frequency": frequency,
                "config": json.dumps(_config(asset_class, record_kind)),
            },
        )
        # An environment without the FinLab scheduler gets it, with its datasets, from seed.
        bind.execute(
            sa.text(
                "INSERT INTO scheduler_dataset (scheduler_key, dataset_key) "
                "SELECT scheduler_key, :key FROM scheduler_control "
                "WHERE scheduler_key = :scheduler ON CONFLICT DO NOTHING"
            ),
            {"key": key, "scheduler": SCHEDULER_KEY},
        )
    bind.execute(
        sa.text(
            "UPDATE source_client SET allowed_datasets = CAST(:staged AS jsonb), updated_at = now() "
            "WHERE lower(trim(source_name)) = 'finlab' AND revoked_at IS NULL "
            "AND jsonb_typeof(allowed_datasets) = 'array' "
            "AND allowed_datasets @> CAST(:previous AS jsonb) "
            "AND allowed_datasets <@ CAST(:previous AS jsonb)"
        ),
        {"staged": json.dumps(STAGED_SCOPE), "previous": json.dumps(PREVIOUS_SCOPE)},
    )


def downgrade() -> None:
    bind = op.get_bind()
    keys = list(DATASETS)
    for table in USAGE_TABLES:
        used = bind.execute(
            sa.text(f"SELECT 1 FROM {table} WHERE dataset_key IN :keys LIMIT 1").bindparams(
                sa.bindparam("keys", expanding=True)
            ),
            {"keys": keys},
        ).first()
        if used:
            raise RuntimeError(
                f"FinLab intraday datasets have been used; downgrade is blocked (rows in {table})"
            )
    bind.execute(
        sa.text(
            "UPDATE source_client SET allowed_datasets = CAST(:previous AS jsonb), updated_at = now() "
            "WHERE lower(trim(source_name)) = 'finlab' "
            "AND jsonb_typeof(allowed_datasets) = 'array' "
            "AND allowed_datasets @> CAST(:staged AS jsonb) "
            "AND allowed_datasets <@ CAST(:staged AS jsonb)"
        ),
        {"staged": json.dumps(STAGED_SCOPE), "previous": json.dumps(PREVIOUS_SCOPE)},
    )
    for statement in (
        "DELETE FROM scheduler_dataset WHERE dataset_key IN :keys",
        "DELETE FROM dataset_registry WHERE dataset_key IN :keys",
    ):
        bind.execute(
            sa.text(statement).bindparams(sa.bindparam("keys", expanding=True)), {"keys": keys}
        )
