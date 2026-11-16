"""Activate the four FinLab intraday datasets.

Revision ID: a9c1e3f5b7d0
Revises: f8b0d2c4e6a9

The previous revision created them inactive, to be activated one by one. No
operation activates a dataset: activation was replaced by the scheduler control
and the published universe baseline, and every governed dataset is active. An
inactive dataset in a scheduler is reported as a configuration error and its
deliveries are refused, so the four could never have been used.

Being active acquires nothing. A dataset is admitted only once its own baseline
is published, the first baseline only comes from an Owner-requested refresh, and
the control has to be started after that.
"""

import sqlalchemy as sa
from alembic import op

revision = "a9c1e3f5b7d0"
down_revision = "f8b0d2c4e6a9"
branch_labels = None
depends_on = None

DATASETS = ["tw_equity_bar_1m", "tw_etf_bar_1m", "tw_equity_tick", "tw_etf_tick"]


def _set_active(active: bool) -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE dataset_registry SET is_active = :active, updated_at = now() "
            "WHERE dataset_key IN :keys AND is_active <> :active"
        ).bindparams(sa.bindparam("keys", expanding=True)),
        {"active": active, "keys": DATASETS},
    )


def upgrade() -> None:
    _set_active(True)


def downgrade() -> None:
    _set_active(False)
