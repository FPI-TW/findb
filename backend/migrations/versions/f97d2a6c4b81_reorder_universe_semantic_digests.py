"""Recompute semantic universe digests with application string ordering.

Revision ID: f97d2a6c4b81
Revises: e86c1f4b2a90
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "f97d2a6c4b81"
down_revision = "e86c1f4b2a90"
branch_labels = None
depends_on = None


def _digest(members: list[dict], *, python_order: bool) -> str:
    if python_order:
        members.sort(key=lambda member: member["member_key"])
    encoded = json.dumps(
        members,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rewrite(*, python_order: bool) -> None:
    bind = op.get_bind()
    release_ids = list(
        bind.execute(
            sa.text("SELECT release_id FROM universe_release ORDER BY release_id")
        ).scalars()
    )
    for release_id in release_ids:
        rows = bind.execute(
            sa.text(
                """
                SELECT member_key, symbol, provider_symbol, exchange, currency,
                       asset_class, classification, contract_code, product_code,
                       contract_month, session, mapping_status
                FROM universe_member
                WHERE release_id=:release_id
                ORDER BY member_key
                """
            ),
            {"release_id": release_id},
        ).mappings()
        bind.execute(
            sa.text(
                "UPDATE universe_release SET semantic_member_sha256=:digest "
                "WHERE release_id=:release_id"
            ),
            {
                "release_id": release_id,
                "digest": _digest([dict(row) for row in rows], python_order=python_order),
            },
        )


def upgrade() -> None:
    _rewrite(python_order=True)


def downgrade() -> None:
    _rewrite(python_order=False)
