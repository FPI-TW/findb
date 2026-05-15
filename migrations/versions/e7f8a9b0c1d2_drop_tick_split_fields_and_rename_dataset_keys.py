"""drop_tick_split_fields_and_rename_dataset_keys

移除 market_data_eod 的 up_volume/down_volume/up_ticks/down_ticks 四個欄位，
並將 dataset_key 由來源相依改為來源中立：
  - tw_equity_multicharts_eod → tw_equity_eod
  - wtx_bloomberg_eod         → wtx_eod
另新增 tw_etf_eod 資料集，供 FinLab ETF 來源使用。

Revision ID: e7f8a9b0c1d2
Revises: d8b9c6b4d732
Create Date: 2026-05-15 09:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = "d8b9c6b4d732"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DATASET_RENAMES = (
    # 先把 Bloomberg 既有的 tw_equity_eod 改名，騰出 key 空間給 FinLab。
    ("tw_equity_eod", "tw_equity_bloomberg_eod"),
    ("tw_equity_multicharts_eod", "tw_equity_eod"),
    ("wtx_bloomberg_eod", "wtx_eod"),
)

_NEW_DATASETS = (
    {
        "dataset_key": "tw_equity_eod",
        "name": "台股日K — FinLab Direct",
        "description": "FinLab Direct 格式台股每日 OHLCV 資料",
        "asset_class": "equity",
        "market": "TW",
        "frequency": "daily",
        "is_active": True,
        "config": '{"source_format": "finlab_twstock_direct"}',
    },
    {
        "dataset_key": "tw_etf_eod",
        "name": "台股 ETF 日K — FinLab Direct",
        "description": "FinLab Direct 格式台股 ETF 每日 OHLCV 資料",
        "asset_class": "etf",
        "market": "TW",
        "frequency": "daily",
        "is_active": True,
        "config": '{"source_format": "finlab_twstock_direct"}',
    },
    {
        "dataset_key": "wtx_eod",
        "name": "WTX 期貨日K",
        "description": "台灣加權指數期貨每日 OHLCV 資料（支援 FinLab 與 Bloomberg 來源）",
        "asset_class": "future",
        "market": "WTX",
        "frequency": "daily",
        "is_active": True,
        "config": '{"source_format": "direct"}',
    },
)


def _rename_dataset_key(old_key: str, new_key: str) -> None:
    """Safely rename a dataset_key by inserting new row, updating FKs, deleting old."""
    bind = op.get_bind()

    old_exists = bind.execute(
        sa.text("SELECT 1 FROM dataset_registry WHERE dataset_key = :k"),
        {"k": old_key},
    ).scalar()
    if not old_exists:
        return

    bind.execute(
        sa.text("""
            INSERT INTO dataset_registry (
                dataset_key, name, description, asset_class, market,
                frequency, is_active, config, created_at, updated_at
            )
            SELECT
                :new_key, name, description, asset_class, market,
                frequency, is_active, config, created_at, updated_at
            FROM dataset_registry
            WHERE dataset_key = :old_key
            ON CONFLICT (dataset_key) DO NOTHING
            """),
        {"new_key": new_key, "old_key": old_key},
    )
    bind.execute(
        sa.text("UPDATE ingestion_run SET dataset_key = :new_key WHERE dataset_key = :old_key"),
        {"new_key": new_key, "old_key": old_key},
    )
    bind.execute(
        sa.text(
            "UPDATE raw.market_payload SET dataset_key = :new_key WHERE dataset_key = :old_key"
        ),
        {"new_key": new_key, "old_key": old_key},
    )
    bind.execute(
        sa.text("DELETE FROM dataset_registry WHERE dataset_key = :old_key"),
        {"old_key": old_key},
    )


def upgrade() -> None:
    op.drop_column("market_data_eod", "down_ticks")
    op.drop_column("market_data_eod", "up_ticks")
    op.drop_column("market_data_eod", "down_volume")
    op.drop_column("market_data_eod", "up_volume")

    for old_key, new_key in _DATASET_RENAMES:
        _rename_dataset_key(old_key, new_key)

    bind = op.get_bind()
    for dataset in _NEW_DATASETS:
        bind.execute(
            sa.text("""
                INSERT INTO dataset_registry (
                    dataset_key, name, description, asset_class, market,
                    frequency, is_active, config, created_at, updated_at
                )
                VALUES (
                    :dataset_key, :name, :description, :asset_class, :market,
                    :frequency, :is_active, CAST(:config AS jsonb), NOW(), NOW()
                )
                ON CONFLICT (dataset_key) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    asset_class = EXCLUDED.asset_class,
                    market = EXCLUDED.market,
                    frequency = EXCLUDED.frequency,
                    is_active = EXCLUDED.is_active,
                    config = EXCLUDED.config,
                    updated_at = NOW()
                """),
            dataset,
        )


def downgrade() -> None:
    bind = op.get_bind()

    for old_key, new_key in reversed(_DATASET_RENAMES):
        _rename_dataset_key(new_key, old_key)

    bind.execute(
        sa.text("DELETE FROM dataset_registry WHERE dataset_key = :k"),
        {"k": "tw_etf_eod"},
    )

    op.add_column("market_data_eod", sa.Column("up_volume", sa.BigInteger(), nullable=True))
    op.add_column("market_data_eod", sa.Column("down_volume", sa.BigInteger(), nullable=True))
    op.add_column("market_data_eod", sa.Column("up_ticks", sa.BigInteger(), nullable=True))
    op.add_column("market_data_eod", sa.Column("down_ticks", sa.BigInteger(), nullable=True))
