"""Storage for the exchanges' three institutional investors tables (三大法人).

Revision ID: 37ed18886cab
Revises: 208a9707fe3f

TWSE and TPEx each publish a market summary in TWD and per-security flows in
shares per trading day. ``institutional_flow_publication`` records the stored
version of each table; ``institutional_flow_summary`` and
``institutional_flow_stock`` hold its rows.

All tables are new and no dataset writes them yet, so a previous release
ignores them. Downgrade is refused once any table is stored.
"""

import sqlalchemy as sa
from alembic import op

revision = "37ed18886cab"
down_revision = "208a9707fe3f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "institutional_flow_publication",
        sa.Column("publication_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("board", sa.String(length=4), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("dataset_key", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("source_symbol", sa.String(length=100), nullable=True),
        sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_raw_sha256", sa.String(length=64), nullable=False),
        sa.Column("rows_sha256", sa.String(length=64), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(kind = 'stock') = (rows_sha256 IS NOT NULL)",
            name=op.f("ck_institutional_flow_publication_rows_object"),
        ),
        sa.CheckConstraint(
            "board IN ('TWSE', 'TPEX')", name=op.f("ck_institutional_flow_publication_board")
        ),
        sa.CheckConstraint(
            "kind IN ('summary', 'stock')", name=op.f("ck_institutional_flow_publication_kind")
        ),
        sa.CheckConstraint(
            "source_raw_sha256 ~ '^[0-9a-f]{64}$' AND (rows_sha256 IS NULL OR rows_sha256 ~ '^[0-9a-f]{64}$')",
            name=op.f("ck_institutional_flow_publication_sha256"),
        ),
        sa.CheckConstraint("row_count >= 1", name=op.f("ck_institutional_flow_publication_rows")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_institutional_flow_publication_run_id_ingestion_run"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("publication_id", name=op.f("pk_institutional_flow_publication")),
        sa.UniqueConstraint(
            "kind", "board", "trade_date", name="uq_institutional_flow_publication"
        ),
    )
    op.create_index(
        "idx_institutional_flow_publication_date",
        "institutional_flow_publication",
        ["trade_date"],
        unique=False,
    )
    op.create_index(
        "idx_institutional_flow_publication_run",
        "institutional_flow_publication",
        ["run_id"],
        unique=False,
    )
    op.create_table(
        "institutional_flow_stock",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("board", sa.String(length=4), nullable=False),
        sa.Column("symbol", sa.String(length=6), nullable=False),
        sa.Column("publication_id", sa.UUID(), nullable=False),
        sa.Column("instrument_id", sa.UUID(), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("foreign_ex_dealer_buy", sa.BigInteger(), nullable=False),
        sa.Column("foreign_ex_dealer_sell", sa.BigInteger(), nullable=False),
        sa.Column("foreign_ex_dealer_net", sa.BigInteger(), nullable=False),
        sa.Column("foreign_dealer_buy", sa.BigInteger(), nullable=False),
        sa.Column("foreign_dealer_sell", sa.BigInteger(), nullable=False),
        sa.Column("foreign_dealer_net", sa.BigInteger(), nullable=False),
        sa.Column("foreign_total_buy", sa.BigInteger(), nullable=True),
        sa.Column("foreign_total_sell", sa.BigInteger(), nullable=True),
        sa.Column("foreign_total_net", sa.BigInteger(), nullable=True),
        sa.Column("investment_trust_buy", sa.BigInteger(), nullable=False),
        sa.Column("investment_trust_sell", sa.BigInteger(), nullable=False),
        sa.Column("investment_trust_net", sa.BigInteger(), nullable=False),
        sa.Column("dealer_proprietary_buy", sa.BigInteger(), nullable=False),
        sa.Column("dealer_proprietary_sell", sa.BigInteger(), nullable=False),
        sa.Column("dealer_proprietary_net", sa.BigInteger(), nullable=False),
        sa.Column("dealer_hedge_buy", sa.BigInteger(), nullable=False),
        sa.Column("dealer_hedge_sell", sa.BigInteger(), nullable=False),
        sa.Column("dealer_hedge_net", sa.BigInteger(), nullable=False),
        sa.Column("dealer_total_buy", sa.BigInteger(), nullable=True),
        sa.Column("dealer_total_sell", sa.BigInteger(), nullable=True),
        sa.Column("dealer_total_net", sa.BigInteger(), nullable=False),
        sa.Column("total_net", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "board IN ('TWSE', 'TPEX')", name=op.f("ck_institutional_flow_stock_board")
        ),
        sa.CheckConstraint(
            "symbol ~ '^[0-9A-Z]{4,6}$'", name=op.f("ck_institutional_flow_stock_symbol")
        ),
        sa.CheckConstraint(
            "(foreign_ex_dealer_buy IS NULL OR foreign_ex_dealer_buy >= 0) AND (foreign_ex_dealer_sell IS NULL OR foreign_ex_dealer_sell >= 0) AND (foreign_dealer_buy IS NULL OR foreign_dealer_buy >= 0) AND (foreign_dealer_sell IS NULL OR foreign_dealer_sell >= 0) AND (foreign_total_buy IS NULL OR foreign_total_buy >= 0) AND (foreign_total_sell IS NULL OR foreign_total_sell >= 0) AND (investment_trust_buy IS NULL OR investment_trust_buy >= 0) AND (investment_trust_sell IS NULL OR investment_trust_sell >= 0) AND (dealer_proprietary_buy IS NULL OR dealer_proprietary_buy >= 0) AND (dealer_proprietary_sell IS NULL OR dealer_proprietary_sell >= 0) AND (dealer_hedge_buy IS NULL OR dealer_hedge_buy >= 0) AND (dealer_hedge_sell IS NULL OR dealer_hedge_sell >= 0) AND (dealer_total_buy IS NULL OR dealer_total_buy >= 0) AND (dealer_total_sell IS NULL OR dealer_total_sell >= 0)",
            name=op.f("ck_institutional_flow_stock_volumes"),
        ),
        sa.CheckConstraint(
            "(foreign_total_buy IS NULL) = (foreign_total_sell IS NULL) AND (foreign_total_buy IS NULL) = (foreign_total_net IS NULL) AND (dealer_total_buy IS NULL) = (dealer_total_sell IS NULL)",
            name=op.f("ck_institutional_flow_stock_subtotals"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_institutional_flow_stock_instrument_id_instruments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["institutional_flow_publication.publication_id"],
            name=op.f("fk_institutional_flow_stock_publication_id_institutional_flow_publication"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "trade_date", "board", "symbol", name=op.f("pk_institutional_flow_stock")
        ),
    )
    op.create_index(
        "idx_institutional_flow_stock_instrument",
        "institutional_flow_stock",
        ["instrument_id", "trade_date"],
        unique=False,
    )
    op.create_index(
        "idx_institutional_flow_stock_publication",
        "institutional_flow_stock",
        ["publication_id"],
        unique=False,
    )
    op.create_index(
        "idx_institutional_flow_stock_rank",
        "institutional_flow_stock",
        ["trade_date", "board", "total_net"],
        unique=False,
    )
    op.create_index(
        "idx_institutional_flow_stock_symbol",
        "institutional_flow_stock",
        ["symbol", "trade_date"],
        unique=False,
    )
    op.create_table(
        "institutional_flow_summary",
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("board", sa.String(length=4), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("publication_id", sa.UUID(), nullable=False),
        sa.Column("buy_amount", sa.NUMERIC(precision=20, scale=0), nullable=False),
        sa.Column("sell_amount", sa.NUMERIC(precision=20, scale=0), nullable=False),
        sa.Column("net_amount", sa.NUMERIC(precision=20, scale=0), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.CheckConstraint(
            "board IN ('TWSE', 'TPEX')", name=op.f("ck_institutional_flow_summary_board")
        ),
        sa.CheckConstraint(
            "category IN ('foreign_ex_dealer', 'foreign_dealer', 'investment_trust', 'dealer_proprietary', 'dealer_hedge', 'foreign_total', 'dealer_total', 'total')",
            name=op.f("ck_institutional_flow_summary_category"),
        ),
        sa.CheckConstraint("currency = 'TWD'", name=op.f("ck_institutional_flow_summary_currency")),
        sa.CheckConstraint(
            "buy_amount >= 0 AND sell_amount >= 0",
            name=op.f("ck_institutional_flow_summary_amounts"),
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["institutional_flow_publication.publication_id"],
            name=op.f(
                "fk_institutional_flow_summary_publication_id_institutional_flow_publication"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "trade_date", "board", "category", name=op.f("pk_institutional_flow_summary")
        ),
    )
    op.create_index(
        "idx_institutional_flow_summary_publication",
        "institutional_flow_summary",
        ["publication_id"],
        unique=False,
    )


def downgrade() -> None:
    stored = op.get_bind().execute(sa.text("SELECT 1 FROM institutional_flow_publication LIMIT 1"))
    if stored.first() is not None:
        raise RuntimeError("institutional flow tables hold published data; refusing to drop them")
    op.drop_index(
        "idx_institutional_flow_summary_publication", table_name="institutional_flow_summary"
    )
    op.drop_table("institutional_flow_summary")
    op.drop_index("idx_institutional_flow_stock_symbol", table_name="institutional_flow_stock")
    op.drop_index("idx_institutional_flow_stock_rank", table_name="institutional_flow_stock")
    op.drop_index("idx_institutional_flow_stock_publication", table_name="institutional_flow_stock")
    op.drop_index("idx_institutional_flow_stock_instrument", table_name="institutional_flow_stock")
    op.drop_table("institutional_flow_stock")
    op.drop_index(
        "idx_institutional_flow_publication_run", table_name="institutional_flow_publication"
    )
    op.drop_index(
        "idx_institutional_flow_publication_date", table_name="institutional_flow_publication"
    )
    op.drop_table("institutional_flow_publication")
