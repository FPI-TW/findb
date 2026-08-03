"""activate_shioaji_pilot_datasets

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DATASETS = (
    {
        "dataset_key": "tw_equity_minute",
        "name": "台股股票分鐘 K — Shioaji",
        "description": "台灣股票一分鐘 OHLCV 資料；production scheduler 目前限已審核 pilot universe。",
        "asset_class": "equity",
    },
    {
        "dataset_key": "tw_etf_minute",
        "name": "台股 ETF 分鐘 K — Shioaji",
        "description": "台灣 ETF 一分鐘 OHLCV 資料；production scheduler 目前限已審核 pilot universe。",
        "asset_class": "etf",
    },
)


def _config(asset_class: str) -> dict[str, object]:
    return {
        "schema_id": "market_minute",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "audit",
        "defaults": {
            "market": "TW",
            "asset_class": asset_class,
            "currency": "TWD",
            "market_timezone": "Asia/Taipei",
            "price_adjustment": "none",
        },
        "governance": {
            "sequence_symbol_limit": 50,
            "sequence_row_limit": 15000,
            "request_rate_limit": {"requests": 50, "window_seconds": 60},
            "request_max_attempts": 3,
            "universe_change_limit": {
                "absolute": 20,
                "ratio": 0.02,
                "fail_when_either_exceeded": True,
            },
            "schedule": {
                "timezone": "Asia/Taipei",
                "universe_refresh": "14:00:00",
                "acquisition_start": "14:30:00",
                "retry_deadline": "17:00:00",
            },
        },
        "source_format": "shioaji_tw_minute",
    }


def upgrade() -> None:
    """Create missing registry rows and enable only the reviewed pilot datasets."""
    statement = sa.text(
        """
        INSERT INTO dataset_registry (
            dataset_key, name, description, asset_class, market, frequency,
            is_active, config, created_at, updated_at
        ) VALUES (
            :dataset_key, :name, :description, :asset_class, 'TW', 'minute',
            true, CAST(:config AS jsonb), now(), now()
        )
        ON CONFLICT (dataset_key) DO UPDATE
        SET description = EXCLUDED.description,
            is_active = true,
            updated_at = now()
        """
    )
    for dataset in _DATASETS:
        op.execute(
            statement.bindparams(
                **dataset,
                config=json.dumps(_config(str(dataset["asset_class"])), ensure_ascii=False),
            )
        )


def downgrade() -> None:
    """Fail closed if the production-pilot scheduler migration is rolled back."""
    op.execute(
        sa.text(
            """
            UPDATE dataset_registry
            SET is_active = false,
                updated_at = now()
            WHERE dataset_key IN :dataset_keys
            """
        ).bindparams(
            sa.bindparam(
                "dataset_keys",
                expanding=True,
                value=tuple(dataset["dataset_key"] for dataset in _DATASETS),
            )
        )
    )
