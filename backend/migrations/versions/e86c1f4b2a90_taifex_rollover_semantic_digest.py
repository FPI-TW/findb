"""Add semantic universe membership identity for dated TAIFEX rollover.

Revision ID: e86c1f4b2a90
Revises: d75fc12a9b43
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "e86c1f4b2a90"
down_revision = "d75fc12a9b43"
branch_labels = None
depends_on = None


def _digest(members: list[dict]) -> str:
    encoded = json.dumps(
        members,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def upgrade() -> None:
    op.add_column(
        "universe_release",
        sa.Column("semantic_member_sha256", sa.String(length=64), nullable=True),
    )
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
            {"release_id": release_id, "digest": _digest([dict(row) for row in rows])},
        )
    op.alter_column("universe_release", "semantic_member_sha256", nullable=False)
    op.create_check_constraint(
        "universe_release_semantic_digest_valid",
        "universe_release",
        "semantic_member_sha256 ~ '^[0-9a-f]{64}$'",
    )
    op.create_index(
        "idx_universe_release_semantic",
        "universe_release",
        ["dataset_key", "provider", "effective_date", "semantic_member_sha256"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_universe_release_semantic", table_name="universe_release")
    op.drop_constraint(
        "universe_release_semantic_digest_valid",
        "universe_release",
        type_="check",
    )
    op.drop_column("universe_release", "semantic_member_sha256")
