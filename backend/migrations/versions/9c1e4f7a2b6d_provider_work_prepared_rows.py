"""Store each retained prepared delivery as its own row.

Revision ID: 9c1e4f7a2b6d
Revises: 3a80c2e1d5f4

The per-work JSONB object was rewritten in full for every new intent/confirm and
read in full by fence/claim/enqueue. Rows keep the exact original bytes and
descriptors; both directions verify a lossless rebuild before dropping anything.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "9c1e4f7a2b6d"
down_revision = "3a80c2e1d5f4"
branch_labels = None
depends_on = None

# The JSONB object each work held, rebuilt from its rows (body omitted when absent).
REBUILT = """SELECT work_id, jsonb_object_agg(
        delivery_key,
        CASE WHEN body IS NULL THEN descriptor
             ELSE descriptor || jsonb_build_object('body', body) END
    ) AS rebuilt
    FROM provider_work_prepared GROUP BY work_id"""


def upgrade():
    op.create_table(
        "provider_work_prepared",
        sa.Column("work_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_key", sa.Text(), nullable=False),
        sa.Column("descriptor", pg.JSONB(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("work_id", "delivery_key", name="pk_provider_work_prepared"),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["provider_work.work_id"],
            name="fk_provider_work_prepared_work_id_provider_work",
            ondelete="RESTRICT",
        ),
    )
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """INSERT INTO provider_work_prepared (work_id, delivery_key, descriptor, body)
            SELECT w.work_id, entry.key, entry.value - 'body', entry.value->>'body'
            FROM provider_work w, jsonb_each(w.prepared_deliveries) AS entry"""
        )
    )
    changed = bind.execute(
        sa.text(
            f"""SELECT count(*) FROM provider_work w
            LEFT JOIN ({REBUILT}) r ON r.work_id = w.work_id
            WHERE COALESCE(r.rebuilt, '{{}}'::jsonb) <> w.prepared_deliveries"""
        )
    ).scalar_one()
    if changed:
        raise RuntimeError(f"{changed} provider work prepared objects did not round-trip")
    op.drop_column("provider_work", "prepared_deliveries")


def downgrade():
    op.add_column(
        "provider_work",
        sa.Column(
            "prepared_deliveries",
            pg.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f"""UPDATE provider_work w SET prepared_deliveries = r.rebuilt
            FROM ({REBUILT}) r WHERE r.work_id = w.work_id"""
        )
    )
    restored, rows = bind.execute(
        sa.text(
            """SELECT
                (SELECT count(*) FROM provider_work, jsonb_each(prepared_deliveries)),
                (SELECT count(*) FROM provider_work_prepared)"""
        )
    ).one()
    if restored != rows:
        raise RuntimeError("provider work prepared rows did not round-trip to JSONB")
    op.drop_table("provider_work_prepared")
