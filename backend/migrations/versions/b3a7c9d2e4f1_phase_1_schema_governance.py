"""phase_1_schema_governance

Revision ID: b3a7c9d2e4f1
Revises: e7f8a9b0c1d2
Create Date: 2026-07-09 12:00:00.000000
"""

from datetime import date
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3a7c9d2e4f1"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ASSET_CLASS_PATTERN = r"^[a-z][a-z0-9_]{1,19}$"
MARKET_PATTERN = r"^[A-Z][A-Z0-9_]{1,9}$"

EOD_COLUMNS = (
    "instrument_id",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "total_ticks",
    "turnover",
    "source",
    "asof_ts",
    "run_id",
    "created_at",
    "updated_at",
)


def _raise_duplicate_error(label: str, rows: list[sa.RowMapping]) -> None:
    if not rows:
        return
    sample = "; ".join(str(dict(row)) for row in rows)
    raise RuntimeError(
        f"Cannot normalize {label} because case/whitespace variants would collide. "
        f"Merge duplicate rows before running this migration. Sample duplicates: {sample}"
    )


def _raise_invalid_error(label: str, rows: list[sa.RowMapping]) -> None:
    if not rows:
        return
    sample = "; ".join(f"{row['field']}={row['value']!r}" for row in rows)
    raise RuntimeError(
        f"Cannot add {label} constraints; clean unsupported formats first. "
        f"Expected asset_class={ASSET_CLASS_PATTERN}, market={MARKET_PATTERN}. "
        f"Sample invalid values: {sample}"
    )


def _check_normalization_collisions() -> None:
    bind = op.get_bind()
    instrument_duplicates = list(bind.execute(sa.text("""
                SELECT
                    lower(trim(asset_class)) AS asset_class,
                    upper(trim(market)) AS market,
                    symbol,
                    COUNT(*) AS duplicate_count
                FROM instruments
                GROUP BY lower(trim(asset_class)), upper(trim(market)), symbol
                HAVING COUNT(*) > 1
                ORDER BY duplicate_count DESC, asset_class, market, symbol
                LIMIT 10
                """)).mappings())
    _raise_duplicate_error("instruments", instrument_duplicates)

    calendar_duplicates = list(bind.execute(sa.text("""
                SELECT
                    upper(trim(market)) AS market,
                    trade_date,
                    COUNT(*) AS duplicate_count
                FROM trading_calendar
                GROUP BY upper(trim(market)), trade_date
                HAVING COUNT(*) > 1
                ORDER BY duplicate_count DESC, market, trade_date
                LIMIT 10
                """)).mappings())
    _raise_duplicate_error("trading_calendar", calendar_duplicates)


def _normalize_and_validate_vocabulary() -> None:
    bind = op.get_bind()
    _check_normalization_collisions()

    bind.execute(sa.text("UPDATE instruments SET asset_class = lower(trim(asset_class))"))
    bind.execute(sa.text("UPDATE instruments SET market = upper(trim(market))"))
    bind.execute(sa.text("UPDATE dataset_registry SET asset_class = lower(trim(asset_class))"))
    bind.execute(sa.text("UPDATE dataset_registry SET market = upper(trim(market))"))
    bind.execute(sa.text("UPDATE trading_calendar SET market = upper(trim(market))"))
    bind.execute(
        sa.text("UPDATE macro_series SET market = upper(trim(market)) WHERE market IS NOT NULL")
    )

    invalid = list(
        bind.execute(
            sa.text("""
                SELECT 'instruments.asset_class' AS field, asset_class AS value
                FROM instruments
                WHERE asset_class !~ :asset_class_pattern
                UNION
                SELECT 'dataset_registry.asset_class' AS field, asset_class AS value
                FROM dataset_registry
                WHERE asset_class !~ :asset_class_pattern
                UNION
                SELECT 'instruments.market' AS field, market AS value
                FROM instruments
                WHERE market !~ :market_pattern
                UNION
                SELECT 'dataset_registry.market' AS field, market AS value
                FROM dataset_registry
                WHERE market !~ :market_pattern
                UNION
                SELECT 'trading_calendar.market' AS field, market AS value
                FROM trading_calendar
                WHERE market !~ :market_pattern
                UNION
                SELECT 'macro_series.market' AS field, market AS value
                FROM macro_series
                WHERE market IS NOT NULL AND market !~ :market_pattern
                ORDER BY field, value
                LIMIT 20
                """),
            {
                "asset_class_pattern": ASSET_CLASS_PATTERN,
                "market_pattern": MARKET_PATTERN,
            },
        ).mappings()
    )
    _raise_invalid_error("vocabulary", invalid)


def _add_vocabulary_constraints() -> None:
    op.create_check_constraint(
        op.f("ck_instruments_instrument_asset_class_valid"),
        "instruments",
        f"asset_class ~ '{ASSET_CLASS_PATTERN}'",
    )
    op.create_check_constraint(
        op.f("ck_instruments_instrument_market_valid"),
        "instruments",
        f"market ~ '{MARKET_PATTERN}'",
    )
    op.create_check_constraint(
        op.f("ck_dataset_registry_dataset_registry_asset_class_valid"),
        "dataset_registry",
        f"asset_class ~ '{ASSET_CLASS_PATTERN}'",
    )
    op.create_check_constraint(
        op.f("ck_dataset_registry_dataset_registry_market_valid"),
        "dataset_registry",
        f"market ~ '{MARKET_PATTERN}'",
    )
    op.create_check_constraint(
        op.f("ck_trading_calendar_calendar_market_valid"),
        "trading_calendar",
        f"market ~ '{MARKET_PATTERN}'",
    )
    op.create_check_constraint(
        op.f("ck_macro_series_macro_series_market_valid"),
        "macro_series",
        f"market IS NULL OR market ~ '{MARKET_PATTERN}'",
    )


def _drop_vocabulary_constraints() -> None:
    constraints = (
        ("instruments", "ck_instruments_instrument_asset_class_valid"),
        ("instruments", "ck_instruments_instrument_market_valid"),
        ("dataset_registry", "ck_dataset_registry_dataset_registry_asset_class_valid"),
        ("dataset_registry", "ck_dataset_registry_dataset_registry_market_valid"),
        ("trading_calendar", "ck_trading_calendar_calendar_market_valid"),
        ("macro_series", "ck_macro_series_macro_series_market_valid"),
    )
    for table, constraint in constraints:
        op.drop_constraint(op.f(constraint), table, type_="check")


def _rewrite_eod_correction_ids() -> None:
    op.execute("""
        UPDATE canonical_correction
        SET record_id = md5(
            'market_data_eod:' || instrument_id::text || ':' || trade_date::text
        )::uuid
        WHERE table_name = 'market_data_eod'
          AND instrument_id IS NOT NULL
          AND trade_date IS NOT NULL
        """)


def _create_market_data_eod_partitioned() -> None:
    op.create_table(
        "market_data_eod",
        sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("open", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("high", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("low", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("close", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("total_ticks", sa.BigInteger(), nullable=True),
        sa.Column("turnover", sa.NUMERIC(precision=20, scale=4), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("asof_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_market_data_eod_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint("instrument_id", "trade_date", name=op.f("pk_market_data_eod")),
        postgresql_partition_by="RANGE (trade_date)",
    )
    op.create_index("idx_eod_date", "market_data_eod", ["trade_date"], unique=False)


def _create_market_data_eod_heap() -> None:
    op.create_table(
        "market_data_eod",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("open", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("high", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("low", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("close", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("total_ticks", sa.BigInteger(), nullable=True),
        sa.Column("turnover", sa.NUMERIC(precision=20, scale=4), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("asof_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_market_data_eod_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_data_eod")),
        sa.UniqueConstraint("instrument_id", "trade_date", name="uq_eod"),
    )
    op.create_index("idx_eod_date", "market_data_eod", ["trade_date"], unique=False)
    op.create_index(
        "idx_eod_inst_date", "market_data_eod", ["instrument_id", "trade_date"], unique=False
    )


def _drop_old_eod_objects(table_name: str) -> None:
    bind = op.get_bind()
    for constraint in (
        "pk_market_data_eod",
        "uq_eod",
        "fk_market_data_eod_instrument_id_instruments",
    ):
        bind.execute(
            sa.text(f'ALTER TABLE "{table_name}" DROP CONSTRAINT IF EXISTS "{constraint}"')
        )
    for index in ("idx_eod_inst_date", "idx_eod_date"):
        bind.execute(sa.text(f'DROP INDEX IF EXISTS "{index}"'))


def _create_eod_partitions_from_old() -> None:
    bind = op.get_bind()
    bounds = bind.execute(sa.text("""
            SELECT
                EXTRACT(YEAR FROM min(trade_date))::int AS min_year,
                EXTRACT(YEAR FROM max(trade_date))::int AS max_year
            FROM market_data_eod_old
            """)).mappings().one()
    min_year = bounds["min_year"]
    max_year = bounds["max_year"]
    current_year = date.today().year
    first_year = min_year if min_year is not None else current_year
    last_year = max(max_year or current_year, current_year + 5)
    for year in range(first_year, last_year + 1):
        op.execute(f"""
                CREATE TABLE IF NOT EXISTS market_data_eod_y{year}
                PARTITION OF market_data_eod
                FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')
                """)
    op.execute(
        "CREATE TABLE IF NOT EXISTS market_data_eod_default " "PARTITION OF market_data_eod DEFAULT"
    )


def upgrade() -> None:
    _normalize_and_validate_vocabulary()
    _add_vocabulary_constraints()
    _rewrite_eod_correction_ids()

    op.rename_table("market_data_eod", "market_data_eod_old")
    _drop_old_eod_objects("market_data_eod_old")
    _create_market_data_eod_partitioned()
    _create_eod_partitions_from_old()
    columns = ", ".join(EOD_COLUMNS)
    op.execute(f"""
        INSERT INTO market_data_eod ({columns})
        SELECT {columns}
        FROM market_data_eod_old
        """)
    op.drop_table("market_data_eod_old")


def downgrade() -> None:
    op.rename_table("market_data_eod", "market_data_eod_partitioned")
    _drop_old_eod_objects("market_data_eod_partitioned")
    _create_market_data_eod_heap()
    columns = ", ".join(EOD_COLUMNS)
    op.execute(f"""
        INSERT INTO market_data_eod (id, {columns})
        SELECT md5('market_data_eod:' || instrument_id::text || ':' || trade_date::text)::uuid,
               {columns}
        FROM market_data_eod_partitioned
        """)
    op.drop_table("market_data_eod_partitioned")
    _drop_vocabulary_constraints()
