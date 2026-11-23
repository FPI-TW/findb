"""Correct the staged dataset descriptions that name a stale provider or contract version.

Revision ID: 44a94f0f8481
Revises: e5a7c9b1d3f2

Seed wrote every staged description as ``<schema>.v1``, so the three
``market_eod`` v2 feeds read v1. ``hk_equity_eod`` was first created for
``twelve_data`` and still names it after FactSet took the feed over. Only a
description still holding one of the values a migration or seed wrote is
replaced; any other text is left alone.

Frozen migration inputs: never import the live catalog or seed. The schedule
inspection used by the deploy preflight stays e5a7c9b1d3f2.

Descriptions are display text that no runtime reads, so downgrade keeps the
corrected values.
"""

import sqlalchemy as sa
from alembic import op

revision = "44a94f0f8481"
down_revision = "e5a7c9b1d3f2"
branch_labels = None
depends_on = None

SUFFIX = " full-market daily feed"
# dataset_key -> (description written before this revision, corrected description)
CORRECTIONS = {
    "hk_equity_eod": (
        (
            "Staged twelve_data market_eod.v1" + SUFFIX,
            "Staged factset market_eod.v1" + SUFFIX,
        ),
        "Staged factset market_eod.v2" + SUFFIX,
    ),
    "hk_index_eod": (("Staged fmp market_eod.v1" + SUFFIX,), "Staged fmp market_eod.v2" + SUFFIX),
    "us_index_eod": (("Staged fmp market_eod.v1" + SUFFIX,), "Staged fmp market_eod.v2" + SUFFIX),
}


def upgrade() -> None:
    statement = sa.text(
        "UPDATE dataset_registry SET description = :corrected, updated_at = now() "
        "WHERE dataset_key = :key AND description = ANY(:previous)"
    )
    for key, (previous, corrected) in CORRECTIONS.items():
        op.execute(statement.bindparams(key=key, previous=list(previous), corrected=corrected))


def downgrade() -> None:
    # Display text only; older releases never read it, so the correction is retained.
    pass
