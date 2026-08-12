"""Repair deny-all scopes for the two active canonical Source credentials.

The staging cutover left the known Twelve Data and FinLab credentials with an
empty JSON array.  This data-only revision restores exactly one canonical
dataset for each provider, but only when the row is active and still has the
exact canonical provider name.  No identity, audit, operator, expiry, or key
material columns are changed.

Rows with a revoked/expired credential, an unknown or non-canonical provider
name, a NULL/malformed scope, or a non-empty scope are deliberately left
untouched.  The downgrade is intentionally a no-op: this revision does not
persist per-row provenance, so changing canonical scopes back to deny-all could
revoke a legitimate operator change made after the upgrade.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Restore the canonical dataset scope for eligible provider credentials."""

    op.execute(
        sa.text(
            """
            UPDATE source_client
            SET allowed_datasets = CASE source_name
                WHEN 'twelve_data' THEN '["us_equity_eod"]'::jsonb
                WHEN 'finlab' THEN '["tw_equity_eod"]'::jsonb
            END
            WHERE source_name IN ('twelve_data', 'finlab')
              AND allowed_datasets = '[]'::jsonb
              AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > now())
            """
        )
    )


def downgrade() -> None:
    """Leave scopes unchanged because upgraded rows have no durable provenance."""

    # There is no safe predicate that distinguishes a scope written by this
    # migration from the same canonical scope set by an operator beforehand.
    # A no-op downgrade is therefore safer than revoking a currently valid
    # credential; the limitation is documented in the module docstring.
    pass
