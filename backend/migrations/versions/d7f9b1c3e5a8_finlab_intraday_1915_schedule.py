"""Move the four FinLab intraday feeds' scheduled start from 18:30 to 19:15 Taipei.

Revision ID: d7f9b1c3e5a8
Revises: e1a3c5e7f9b2

The daily price tables are taken only once FinLab publishes the day's final copy
(around 18:50). A daily work waiting for it retries at the tail of the provider
queue, so intraday works queued at 18:30 would run first and hold the daily for
hours. The Backend publishes the new start from its catalog; this revision only
keeps each dataset's stored (disabled) schedule equal to what seed declares.

Frozen migration inputs: never import the live catalog. A stored start other than
the old or the new one aborts before any update. Downgrade restores 18:30 only
where the start is still this revision's.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "d7f9b1c3e5a8"
down_revision = "e1a3c5e7f9b2"
branch_labels = None
depends_on = None

DATASETS = ("tw_equity_bar_1m", "tw_etf_bar_1m", "tw_equity_tick", "tw_etf_tick")
OLD = "18:30:00"
NEW = "19:15:00"


def _move(source: str, target: str) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT dataset_key, config FROM dataset_registry "
            "WHERE dataset_key IN :keys ORDER BY dataset_key FOR UPDATE"
        ).bindparams(sa.bindparam("keys", expanding=True)),
        {"keys": list(DATASETS)},
    ).mappings()
    updates, unknown = [], []
    for row in rows:
        config = row["config"]
        try:
            schedule = config["delivery_expectation"]["schedule"]
            current = schedule["local_time"]
        except (KeyError, TypeError):
            unknown.append(row["dataset_key"])
            continue
        if current == target:
            continue
        if current != source:
            unknown.append(row["dataset_key"])
            continue
        schedule["local_time"] = target
        updates.append({"key": row["dataset_key"], "config": json.dumps(config)})
    if unknown:
        raise RuntimeError("FinLab intraday schedule unknown differences: " + ", ".join(unknown))
    for update in updates:
        connection.execute(
            sa.text(
                "UPDATE dataset_registry SET config = CAST(:config AS jsonb), updated_at = now() "
                "WHERE dataset_key = :key"
            ),
            update,
        )


def upgrade() -> None:
    _move(OLD, NEW)


def downgrade() -> None:
    _move(NEW, OLD)
