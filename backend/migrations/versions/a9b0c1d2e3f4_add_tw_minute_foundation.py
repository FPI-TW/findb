"""add_tw_minute_foundation

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid_column(name: str, *args: object, **kwargs: object) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kwargs)


def upgrade() -> None:
    op.create_table(
        "market_data_minute",
        _uuid_column(
            "instrument_id",
            sa.ForeignKey("instruments.instrument_id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("trade_date", sa.Date(), primary_key=True, nullable=False),
        sa.Column("bar_start_time", sa.DateTime(timezone=True), primary_key=True, nullable=False),
        sa.Column("bar_end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signal_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_timezone", sa.String(64), nullable=False),
        sa.Column("open", postgresql.NUMERIC(20, 8), nullable=False),
        sa.Column("high", postgresql.NUMERIC(20, 8), nullable=False),
        sa.Column("low", postgresql.NUMERIC(20, 8), nullable=False),
        sa.Column("close", postgresql.NUMERIC(20, 8), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("turnover", postgresql.NUMERIC(20, 4), nullable=True),
        sa.Column("trade_count", sa.BigInteger(), nullable=True),
        sa.Column("price_adjustment", sa.String(10), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_priority", sa.Integer(), nullable=False),
        sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asof_ts", sa.DateTime(timezone=True), nullable=False),
        _uuid_column(
            "run_id",
            sa.ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "bar_end_time = bar_start_time + interval '1 minute'",
            name="market_data_minute_one_minute_interval",
        ),
        sa.CheckConstraint(
            "signal_time = bar_end_time", name="market_data_minute_signal_at_bar_end"
        ),
        sa.CheckConstraint(
            "open >= 0 AND high >= 0 AND low >= 0 AND close >= 0 AND low <= open AND low <= close AND low <= high AND high >= open AND high >= close",
            name="market_data_minute_ohlc_bounds",
        ),
        sa.CheckConstraint(
            "volume IS NULL OR volume >= 0", name="market_data_minute_volume_nonnegative"
        ),
        sa.CheckConstraint(
            "turnover IS NULL OR turnover >= 0", name="market_data_minute_turnover_nonnegative"
        ),
        sa.CheckConstraint("trade_count IS NULL", name="market_data_minute_trade_count_null"),
        sa.CheckConstraint(
            "price_adjustment = 'none'", name="market_data_minute_price_adjustment_none"
        ),
        sa.CheckConstraint(
            "market_timezone = 'Asia/Taipei'", name="market_data_minute_timezone_taipei"
        ),
        sa.CheckConstraint(
            "trade_date = (bar_start_time AT TIME ZONE 'Asia/Taipei')::date",
            name="market_data_minute_local_trade_date",
        ),
        postgresql_partition_by="RANGE (trade_date)",
    )
    op.execute("CREATE TABLE market_data_minute_default PARTITION OF market_data_minute DEFAULT")
    op.create_index(
        "idx_market_data_minute_instrument_bar",
        "market_data_minute",
        ["instrument_id", "bar_start_time"],
    )
    op.create_index("idx_market_data_minute_trade_date", "market_data_minute", ["trade_date"])

    op.create_table(
        "tw_minute_universe_release",
        _uuid_column("release_id", primary_key=True),
        sa.Column(
            "dataset_key",
            sa.String(50),
            sa.ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("shioaji_eligibility_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("shioaji_eligibility_checksum", sa.String(64), nullable=True),
        sa.Column("official_membership_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("official_membership_checksum", sa.String(64), nullable=True),
        sa.Column("member_checksum", sa.String(64), nullable=False),
        sa.Column("previous_member_count", sa.Integer(), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("change_count", sa.Integer(), nullable=False),
        sa.Column("change_ratio", postgresql.NUMERIC(8, 6), nullable=False),
        sa.Column("change_count_threshold", sa.Integer(), nullable=False),
        sa.Column("change_ratio_threshold", postgresql.NUMERIC(8, 6), nullable=False),
        sa.Column("change_audit", postgresql.JSONB(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "dataset_key", "effective_date", "version", name="uq_tw_minute_universe_release"
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'published', 'superseded')",
            name="tw_minute_universe_release_status_valid",
        ),
        sa.CheckConstraint(
            "change_ratio >= 0 AND change_ratio <= 1", name="tw_minute_universe_change_ratio"
        ),
        sa.CheckConstraint("version >= 1", name="tw_minute_universe_version_positive"),
        sa.CheckConstraint(
            "previous_member_count IS NULL OR previous_member_count >= 0",
            name="tw_minute_universe_previous_count_nonnegative",
        ),
        sa.CheckConstraint("member_count >= 0", name="tw_minute_universe_member_count_nonnegative"),
        sa.CheckConstraint(
            "member_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_universe_member_checksum_valid",
        ),
        sa.CheckConstraint("change_count >= 0", name="tw_minute_universe_change_count_nonnegative"),
        sa.CheckConstraint(
            "change_count_threshold = 20", name="tw_minute_universe_count_threshold_fixed"
        ),
        sa.CheckConstraint(
            "change_ratio_threshold = 0.02", name="tw_minute_universe_ratio_threshold_fixed"
        ),
        sa.CheckConstraint(
            "status != 'published' OR COALESCE("
            "jsonb_typeof(shioaji_eligibility_snapshot) = 'object' AND shioaji_eligibility_snapshot <> '{}'::jsonb "
            "AND shioaji_eligibility_snapshot->>'source' = 'shioaji' AND shioaji_eligibility_snapshot ? 'eligibility' "
            "AND jsonb_typeof(shioaji_eligibility_snapshot->'eligibility') IN ('array', 'object') "
            "AND shioaji_eligibility_snapshot->'eligibility' NOT IN ('[]'::jsonb, '{}'::jsonb) "
            "AND jsonb_typeof(official_membership_snapshot) = 'object' AND official_membership_snapshot <> '{}'::jsonb "
            "AND official_membership_snapshot->>'source' IN ('twse', 'tpex', 'twse_tpex') "
            "AND official_membership_snapshot ? 'membership' AND official_membership_snapshot ? 'classification' "
            "AND jsonb_typeof(official_membership_snapshot->'membership') IN ('array', 'object') "
            "AND official_membership_snapshot->'membership' NOT IN ('[]'::jsonb, '{}'::jsonb) "
            "AND jsonb_typeof(official_membership_snapshot->'classification') IN ('array', 'object') "
            "AND official_membership_snapshot->'classification' NOT IN ('[]'::jsonb, '{}'::jsonb) "
            "AND shioaji_eligibility_checksum ~ '^[0-9a-f]{64}$' AND official_membership_checksum ~ '^[0-9a-f]{64}$' "
            "AND shioaji_eligibility_checksum <> official_membership_checksum "
            "AND jsonb_typeof(change_audit) = 'object' AND change_audit <> '{}'::jsonb "
            "AND change_audit ? 'decision' AND change_audit ? 'differences' AND published_at IS NOT NULL, FALSE)",
            name="tw_minute_universe_published_evidence",
        ),
        sa.CheckConstraint(
            "status != 'published' OR (change_count <= 20 AND change_ratio <= 0.02)",
            name="tw_minute_universe_published_within_thresholds",
        ),
    )
    op.create_index(
        "idx_tw_minute_universe_release_lookup",
        "tw_minute_universe_release",
        ["dataset_key", "effective_date", "status"],
    )
    op.create_table(
        "tw_minute_universe_member",
        _uuid_column("member_id", primary_key=True),
        _uuid_column(
            "release_id",
            sa.ForeignKey("tw_minute_universe_release.release_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _uuid_column(
            "instrument_id",
            sa.ForeignKey("instruments.instrument_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("source_symbol", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("release_id", "instrument_id", name="uq_tw_minute_universe_member"),
        sa.CheckConstraint(
            "ended_on IS NULL OR ended_on >= started_on", name="tw_minute_member_date_order"
        ),
    )
    op.create_index(
        "idx_tw_minute_universe_member_instrument",
        "tw_minute_universe_member",
        ["instrument_id", "ended_on"],
    )
    op.create_table(
        "tw_minute_daily_update",
        _uuid_column("update_id", primary_key=True),
        sa.Column("environment", sa.String(30), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "environment", "market", "trade_date", name="uq_tw_minute_daily_update"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'blocked', 'skipped_calendar')",
            name="tw_minute_daily_update_status_valid",
        ),
        sa.CheckConstraint("market = 'TW'", name="tw_minute_daily_update_market_tw"),
    )
    op.create_index(
        "idx_tw_minute_daily_update_status", "tw_minute_daily_update", ["status", "trade_date"]
    )
    op.create_table(
        "tw_minute_dataset_snapshot",
        _uuid_column("snapshot_id", primary_key=True),
        _uuid_column(
            "daily_update_id",
            sa.ForeignKey("tw_minute_daily_update.update_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "dataset_key",
            sa.String(50),
            sa.ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trade_date", sa.Date(), nullable=False),
        _uuid_column(
            "universe_release_id",
            sa.ForeignKey("tw_minute_universe_release.release_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("symbols_checksum", sa.String(64), nullable=False),
        sa.Column("sequence_count", sa.Integer(), nullable=False),
        sa.Column("expected_rows", sa.Integer(), nullable=False),
        sa.Column("received_rows", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("symbol_results", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "daily_update_id", "dataset_key", name="uq_tw_minute_snapshot_daily_dataset"
        ),
        sa.UniqueConstraint(
            "daily_update_id",
            "dataset_key",
            "trade_date",
            "universe_release_id",
            "symbols_checksum",
            name="uq_tw_minute_snapshot_identity",
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'running', 'completed', 'failed', 'partial')",
            name="tw_minute_snapshot_status_valid",
        ),
        sa.CheckConstraint(
            "sequence_count >= 1 AND expected_rows >= 0 AND received_rows >= 0",
            name="tw_minute_snapshot_counts_nonnegative",
        ),
    )
    op.create_index(
        "idx_tw_minute_snapshot_update",
        "tw_minute_dataset_snapshot",
        ["daily_update_id", "dataset_key"],
    )
    op.create_table(
        "tw_minute_snapshot_part",
        _uuid_column("part_id", primary_key=True),
        _uuid_column(
            "snapshot_id",
            sa.ForeignKey("tw_minute_dataset_snapshot.snapshot_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        _uuid_column(
            "ingestion_run_id",
            sa.ForeignKey("ingestion_run.run_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("symbols", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("received_rows", sa.Integer(), nullable=False),
        sa.Column("result_summary", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("snapshot_id", "sequence", name="uq_tw_minute_snapshot_part_sequence"),
        sa.CheckConstraint("sequence >= 1", name="tw_minute_snapshot_part_sequence_positive"),
        sa.CheckConstraint(
            "request_count >= 0 AND received_rows >= 0",
            name="tw_minute_snapshot_part_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="tw_minute_snapshot_part_status_valid",
        ),
    )
    op.create_table(
        "tw_minute_publication_revision",
        _uuid_column("publication_id", primary_key=True),
        _uuid_column(
            "daily_update_id",
            sa.ForeignKey("tw_minute_daily_update.update_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("manifest_checksum", sa.String(64), nullable=False),
        sa.Column("warning_kind", sa.String(30), nullable=True),
        sa.Column("is_latest", sa.Boolean(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "daily_update_id", "revision", name="uq_tw_minute_publication_revision"
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'completed_with_warnings', 'unresolved_gap')",
            name="tw_minute_publication_status_valid",
        ),
        sa.CheckConstraint("revision >= 1", name="tw_minute_publication_revision_positive"),
        sa.CheckConstraint(
            "NOT (is_latest AND status = 'unresolved_gap')",
            name="tw_minute_publication_latest_resolved",
        ),
        sa.CheckConstraint(
            "(status = 'completed_with_warnings' AND warning_kind IS NOT NULL "
            "AND warning_kind = 'expected_no_data') "
            "OR (status != 'completed_with_warnings' AND warning_kind IS NULL)",
            name="tw_minute_publication_warning_kind_coherent",
        ),
        sa.CheckConstraint(
            "status != 'completed_with_warnings' OR COALESCE(jsonb_typeof(manifest) = 'object' "
            "AND jsonb_typeof(manifest->'expected_no_data_symbols') = 'array' "
            "AND jsonb_array_length(manifest->'expected_no_data_symbols') > 0, FALSE)",
            name="tw_minute_publication_warning_manifest_evidence",
        ),
        sa.CheckConstraint(
            "NOT is_latest OR published_at IS NOT NULL",
            name="tw_minute_publication_latest_published",
        ),
    )
    op.create_index(
        "uq_tw_minute_publication_latest",
        "tw_minute_publication_revision",
        ["daily_update_id"],
        unique=True,
        postgresql_where=sa.text("is_latest"),
    )
    op.create_table(
        "tw_minute_archive_release",
        _uuid_column("release_id", primary_key=True),
        sa.Column("release_key", sa.String(100), nullable=False),
        sa.Column(
            "dataset_key",
            sa.String(50),
            sa.ForeignKey("dataset_registry.dataset_key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("upstream_source", sa.String(50), nullable=False),
        sa.Column("overlap_precedence", sa.String(50), nullable=False),
        sa.Column("trading_calendar_checksum", sa.String(64), nullable=False),
        sa.Column("instruments_checksum", sa.String(64), nullable=False),
        sa.Column("trading_dates_checksum", sa.String(64), nullable=False),
        sa.Column("expected_trading_date_count", sa.Integer(), nullable=False),
        sa.Column("covered_trading_date_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("coverage_start", sa.Date(), nullable=False),
        sa.Column("coverage_end", sa.Date(), nullable=False),
        sa.Column("instrument_count", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("sequence_count", sa.Integer(), nullable=False),
        sa.Column("manifest_checksum", sa.String(64), nullable=False),
        sa.Column("content_checksum", sa.String(64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "dataset_key", "source", "manifest_checksum", name="uq_tw_minute_archive_manifest"
        ),
        sa.CheckConstraint("source = 'tw_recorder_archive'", name="tw_minute_archive_source_valid"),
        sa.CheckConstraint(
            "upstream_source = 'shioaji'", name="tw_minute_archive_upstream_source_valid"
        ),
        sa.CheckConstraint(
            "overlap_precedence = 'direct_daily_shioaji'",
            name="tw_minute_archive_overlap_precedence_valid",
        ),
        sa.CheckConstraint(
            "trading_calendar_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_calendar_checksum_valid",
        ),
        sa.CheckConstraint(
            "release_key ~ '^[a-z0-9][a-z0-9._-]*$'",
            name="tw_minute_archive_release_key_valid",
        ),
        sa.CheckConstraint(
            "content_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_content_checksum_valid",
        ),
        sa.CheckConstraint(
            "manifest_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_manifest_checksum_valid",
        ),
        sa.CheckConstraint(
            "coverage_start <= coverage_end", name="tw_minute_archive_coverage_order"
        ),
        sa.CheckConstraint(
            "status IN ('staged', 'finalized', 'failed')", name="tw_minute_archive_status_valid"
        ),
        sa.CheckConstraint(
            "instrument_count >= 1 AND row_count >= 1 AND sequence_count >= 1",
            name="tw_minute_archive_counts_positive",
        ),
        sa.CheckConstraint(
            "(status = 'finalized') = (finalized_at IS NOT NULL)",
            name="tw_minute_archive_finalized_timestamp",
        ),
        sa.CheckConstraint(
            "instruments_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_instruments_checksum_valid",
        ),
        sa.CheckConstraint(
            "trading_dates_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_dates_checksum_valid",
        ),
        sa.CheckConstraint(
            "expected_trading_date_count >= 1 AND covered_trading_date_count >= 1 AND chunk_count >= 1",
            name="tw_minute_archive_manifest_counts_positive",
        ),
    )
    op.create_table(
        "tw_minute_archive_chunk",
        _uuid_column("chunk_id", primary_key=True),
        _uuid_column(
            "release_id",
            sa.ForeignKey("tw_minute_archive_release.release_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_sequence", sa.Integer(), nullable=False),
        sa.Column("chunk_sequence", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("coverage_start", sa.Date(), nullable=False),
        sa.Column("coverage_end", sa.Date(), nullable=False),
        sa.Column("instrument_count", sa.Integer(), nullable=False),
        sa.Column("instrument_checksum", sa.String(64), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("chunk_checksum", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("object_checksum", sa.String(64), nullable=False),
        sa.Column("object_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "release_id",
            "snapshot_sequence",
            "chunk_sequence",
            name="uq_tw_minute_archive_chunk_sequence",
        ),
        sa.CheckConstraint(
            "snapshot_sequence >= 1 AND chunk_sequence >= 1 AND chunk_sequence <= chunk_count",
            name="tw_minute_archive_chunk_sequence_valid",
        ),
        sa.CheckConstraint(
            "instrument_count >= 1", name="tw_minute_archive_chunk_instrument_positive"
        ),
        sa.CheckConstraint(
            "row_count >= 1 AND row_count <= 5000",
            name="tw_minute_archive_chunk_row_count_range",
        ),
        sa.CheckConstraint(
            "object_size_bytes >= 1",
            name="tw_minute_archive_chunk_object_size_positive",
        ),
        sa.CheckConstraint(
            "chunk_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_chunk_checksum_valid",
        ),
        sa.CheckConstraint(
            "object_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_chunk_object_checksum_valid",
        ),
        sa.CheckConstraint(
            "instrument_checksum ~ '^[0-9a-f]{64}$'",
            name="tw_minute_archive_chunk_instrument_checksum_valid",
        ),
        sa.CheckConstraint(
            "coverage_start <= coverage_end", name="tw_minute_archive_chunk_coverage_order"
        ),
        sa.CheckConstraint(
            "date_trunc('month', coverage_start::timestamp)::date = coverage_start",
            name="tw_minute_archive_chunk_start_month",
        ),
        sa.CheckConstraint(
            "date_trunc('month', coverage_end::timestamp)::date = coverage_end",
            name="tw_minute_archive_chunk_end_month",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION validate_tw_minute_archive_finalization() RETURNS trigger AS $$
        DECLARE chunk_total integer; row_total bigint; min_coverage date; max_coverage date;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.status = 'finalized' THEN RAISE EXCEPTION 'finalized archive release is immutable'; END IF;
            RETURN OLD;
          END IF;
          IF TG_OP = 'UPDATE' AND OLD.status = 'finalized' THEN
            RAISE EXCEPTION 'finalized archive release is immutable';
          END IF;
          IF NEW.status <> 'finalized' THEN RETURN NEW; END IF;
          IF NOT COALESCE(
            jsonb_typeof(NEW.manifest) = 'object' AND NEW.manifest <> '{}'::jsonb
            AND NEW.manifest->>'source' = 'tw_recorder_archive'
            AND NEW.manifest->>'upstream_source' = 'shioaji'
            AND NEW.manifest->>'overlap_precedence' = 'direct_daily_shioaji'
            AND NEW.manifest->>'dataset_key' = NEW.dataset_key
            AND NEW.manifest->>'trading_calendar_checksum' = NEW.trading_calendar_checksum
            AND NEW.manifest->>'instruments_sha256' = NEW.instruments_checksum
            AND NEW.manifest->>'trading_dates_sha256' = NEW.trading_dates_checksum
            AND (NEW.manifest->>'coverage_start_month')::date = NEW.coverage_start
            AND (NEW.manifest->>'coverage_end_month')::date = NEW.coverage_end
            AND (NEW.manifest->>'sequence_count')::integer = NEW.sequence_count
            AND (NEW.manifest->>'chunk_count')::integer = NEW.chunk_count
            AND (NEW.manifest->>'row_count')::bigint = NEW.row_count
            AND jsonb_typeof(NEW.manifest->'expected_trading_dates') = 'array'
            AND jsonb_typeof(NEW.manifest->'covered_trading_dates') = 'array'
            AND NEW.manifest->'expected_trading_dates' = NEW.manifest->'covered_trading_dates'
            AND jsonb_array_length(NEW.manifest->'expected_trading_dates') > 0
            AND jsonb_array_length(NEW.manifest->'expected_trading_dates') = NEW.expected_trading_date_count
            AND jsonb_array_length(NEW.manifest->'covered_trading_dates') = NEW.covered_trading_date_count
            AND jsonb_typeof(NEW.manifest->'instruments') = 'array'
            AND jsonb_array_length(NEW.manifest->'instruments') = NEW.instrument_count
            AND jsonb_typeof(NEW.manifest->'chunks') = 'array'
            AND jsonb_array_length(NEW.manifest->'chunks') = NEW.chunk_count
            AND jsonb_typeof(NEW.manifest->'objects') = 'array'
            AND jsonb_array_length(NEW.manifest->'objects') >= 1, FALSE) THEN
            RAISE EXCEPTION 'invalid finalized archive manifest';
          END IF;
          IF NEW.expected_trading_date_count <> NEW.covered_trading_date_count
            OR NEW.manifest->'covered_months' <> (
              SELECT jsonb_agg(to_char(month_value, 'YYYY-MM-DD') ORDER BY month_value)
              FROM generate_series(
                NEW.coverage_start::timestamp,
                NEW.coverage_end::timestamp,
                interval '1 month'
              ) month_value
            )
            OR EXISTS (
              SELECT 1
              FROM (
                SELECT value, lag(value) OVER (ORDER BY ordinality) AS previous_value
                FROM jsonb_array_elements_text(
                  NEW.manifest->'expected_trading_dates'
                ) WITH ORDINALITY AS dates(value, ordinality)
              ) ordered_dates
              WHERE previous_value IS NOT NULL AND value <= previous_value
            )
            OR EXISTS (
              SELECT 1
              FROM jsonb_array_elements_text(NEW.manifest->'expected_trading_dates') value
              WHERE value !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                OR value::date < NEW.coverage_start
                OR value::date >= (NEW.coverage_end + interval '1 month')::date
            )
            OR EXISTS (
              SELECT 1
              FROM generate_series(
                NEW.coverage_start::timestamp,
                NEW.coverage_end::timestamp,
                interval '1 month'
              ) month_value
              WHERE NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements_text(
                  NEW.manifest->'expected_trading_dates'
                ) trading_date
                WHERE date_trunc('month', trading_date::date) = month_value
              )
            )
            OR (
              SELECT count(*) <> count(DISTINCT value)
              FROM jsonb_array_elements_text(NEW.manifest->'instruments') value
            )
            OR (
              SELECT encode(
                sha256(convert_to(string_agg(value, E'\\n' ORDER BY value), 'UTF8')),
                'hex'
              )
              FROM jsonb_array_elements_text(NEW.manifest->'instruments') value
            ) <> NEW.instruments_checksum
            OR (
              SELECT encode(
                sha256(
                  convert_to(
                    string_agg(value, E'\\n' ORDER BY ordinality),
                    'UTF8'
                  )
                ),
                'hex'
              )
              FROM jsonb_array_elements_text(
                NEW.manifest->'expected_trading_dates'
              ) WITH ORDINALITY AS dates(value, ordinality)
            ) <> NEW.trading_dates_checksum THEN
            RAISE EXCEPTION 'archive continuity or checksum evidence is invalid';
          END IF;
          SELECT count(*), coalesce(sum(row_count), 0), min(coverage_start), max(coverage_end)
          INTO chunk_total, row_total, min_coverage, max_coverage
          FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id;
          IF chunk_total <> NEW.chunk_count OR row_total <> NEW.row_count
             OR min_coverage <> NEW.coverage_start OR max_coverage <> NEW.coverage_end THEN
            RAISE EXCEPTION 'archive chunks do not match release totals';
          END IF;
          IF EXISTS (
            SELECT 1 FROM tw_minute_archive_chunk
            WHERE release_id = NEW.release_id
              AND (instrument_count <> NEW.instrument_count OR instrument_checksum <> NEW.instruments_checksum)
          ) OR EXISTS (
            SELECT 1
            FROM tw_minute_archive_chunk persisted
            WHERE persisted.release_id = NEW.release_id
              AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.manifest->'chunks') manifest_chunk
                WHERE (manifest_chunk->>'snapshot_sequence')::integer = persisted.snapshot_sequence
                  AND (manifest_chunk->>'chunk_sequence')::integer = persisted.chunk_sequence
                  AND (manifest_chunk->>'chunk_count')::integer = persisted.chunk_count
                  AND (manifest_chunk->>'row_count')::bigint = persisted.row_count
                  AND manifest_chunk->>'checksum_sha256' = persisted.chunk_checksum
                  AND manifest_chunk->'object'->>'object_key' = persisted.object_key
                  AND manifest_chunk->'object'->>'sha256' = persisted.object_checksum
                  AND (manifest_chunk->'object'->>'byte_size')::bigint = persisted.object_size_bytes
              )
          ) OR EXISTS (
            SELECT 1
            FROM tw_minute_archive_chunk persisted
            WHERE persisted.release_id = NEW.release_id
              AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(NEW.manifest->'objects') manifest_object
                WHERE manifest_object->>'object_key' = persisted.object_key
                  AND manifest_object->>'sha256' = persisted.object_checksum
                  AND (manifest_object->>'byte_size')::bigint = persisted.object_size_bytes
              )
          ) OR EXISTS (
            SELECT 1 FROM (
              SELECT snapshot_sequence, count(*) AS n, min(chunk_sequence) AS lo,
                     max(chunk_sequence) AS hi, min(chunk_count) AS declared_lo, max(chunk_count) AS declared_hi
              FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id GROUP BY snapshot_sequence
            ) grouped
            WHERE n <> declared_lo OR lo <> 1 OR hi <> declared_lo OR declared_lo <> declared_hi
          ) OR (SELECT min(snapshot_sequence) FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id) <> 1
            OR (SELECT max(snapshot_sequence) FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id) <> NEW.sequence_count
            OR (SELECT count(DISTINCT snapshot_sequence) FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id) <> NEW.sequence_count THEN
            RAISE EXCEPTION 'archive chunk sequence or instrument evidence mismatch';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER tw_minute_archive_finalization_guard
        BEFORE INSERT OR UPDATE OR DELETE ON tw_minute_archive_release
        FOR EACH ROW EXECUTE FUNCTION validate_tw_minute_archive_finalization();
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_tw_minute_finalized_archive_chunks() RETURNS trigger AS $$
        DECLARE guarded_release uuid;
        BEGIN
          guarded_release := COALESCE(NEW.release_id, OLD.release_id);
          IF EXISTS (
            SELECT 1 FROM tw_minute_archive_release
            WHERE status = 'finalized'
              AND release_id IN (guarded_release, OLD.release_id)
          ) THEN
            RAISE EXCEPTION 'finalized archive chunks are immutable';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER tw_minute_archive_chunk_immutability_guard
        BEFORE INSERT OR UPDATE OR DELETE ON tw_minute_archive_chunk
        FOR EACH ROW EXECUTE FUNCTION guard_tw_minute_finalized_archive_chunks();
        """
    )
    # The SQL checks above make malformed rows cheap to reject; these transition
    # guards bind the evidence to its relational records, which a CHECK cannot do.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_tw_minute_archive_finalization() RETURNS trigger AS $$
        DECLARE required_keys text[] := ARRAY[
          'schema_id','schema_version','release_id','dataset_key','source','upstream_source',
          'overlap_precedence','trading_calendar_checksum','coverage_start_month',
          'coverage_end_month','covered_months','expected_trading_dates','covered_trading_dates',
          'trading_dates_sha256','instruments','instruments_sha256','sequence_count',
          'chunk_count','row_count','checksum_sha256','chunks','objects','release_state','finalized_at'
        ];
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.status = 'finalized' THEN RAISE EXCEPTION 'finalized archive release is immutable'; END IF;
            RETURN OLD;
          END IF;
          IF TG_OP = 'UPDATE' AND OLD.status = 'finalized' THEN
            RAISE EXCEPTION 'finalized archive release is immutable';
          END IF;
          IF NEW.status <> 'finalized' THEN RETURN NEW; END IF;
          IF jsonb_typeof(NEW.manifest) <> 'object'
             OR (SELECT count(*) FROM jsonb_object_keys(NEW.manifest)) <> cardinality(required_keys)
             OR EXISTS (SELECT 1 FROM unnest(required_keys) key WHERE NOT NEW.manifest ? key)
             OR NEW.manifest_checksum <> encode(sha256(convert_to(NEW.manifest::text, 'UTF8')), 'hex')
             OR NEW.manifest->>'schema_id' <> 'market_minute_archive'
             OR NEW.manifest->>'schema_version' <> '1'
             OR NEW.manifest->>'release_id' <> NEW.release_key
             OR NEW.manifest->>'dataset_key' <> NEW.dataset_key
             OR NEW.manifest->>'source' <> NEW.source
             OR NEW.manifest->>'upstream_source' <> NEW.upstream_source
             OR NEW.manifest->>'overlap_precedence' <> NEW.overlap_precedence
             OR NEW.manifest->>'trading_calendar_checksum' <> NEW.trading_calendar_checksum
             OR NEW.manifest->>'instruments_sha256' <> NEW.instruments_checksum
             OR NEW.manifest->>'trading_dates_sha256' <> NEW.trading_dates_checksum
             OR NEW.manifest->>'checksum_sha256' <> NEW.content_checksum
             OR NEW.manifest->>'release_state' <> 'finalized'
             OR NEW.manifest->>'finalized_at' IS NULL
             OR jsonb_typeof(NEW.manifest->'covered_months') <> 'array'
             OR jsonb_typeof(NEW.manifest->'expected_trading_dates') <> 'array'
             OR jsonb_typeof(NEW.manifest->'covered_trading_dates') <> 'array'
             OR jsonb_typeof(NEW.manifest->'instruments') <> 'array'
             OR jsonb_typeof(NEW.manifest->'chunks') <> 'array'
             OR jsonb_typeof(NEW.manifest->'objects') <> 'array' THEN
            RAISE EXCEPTION 'invalid finalized archive manifest';
          END IF;
          IF (NEW.manifest->>'coverage_start_month') !~ '^\\d{4}-\\d{2}-01$'
             OR (NEW.manifest->>'coverage_end_month') !~ '^\\d{4}-\\d{2}-01$'
             OR (NEW.manifest->>'coverage_start_month')::date <> NEW.coverage_start
             OR (NEW.manifest->>'coverage_end_month')::date <> NEW.coverage_end
             OR (NEW.manifest->>'sequence_count')::integer <> NEW.sequence_count
             OR (NEW.manifest->>'chunk_count')::integer <> NEW.chunk_count
             OR (NEW.manifest->>'row_count')::bigint <> NEW.row_count
             OR (NEW.manifest->>'finalized_at')::timestamptz <> NEW.finalized_at
             OR jsonb_array_length(NEW.manifest->'expected_trading_dates') <> NEW.expected_trading_date_count
             OR jsonb_array_length(NEW.manifest->'covered_trading_dates') <> NEW.covered_trading_date_count
             OR jsonb_array_length(NEW.manifest->'instruments') <> NEW.instrument_count
             OR jsonb_array_length(NEW.manifest->'chunks') <> NEW.chunk_count
             OR NEW.manifest->'expected_trading_dates' <> NEW.manifest->'covered_trading_dates'
             OR NEW.manifest->'covered_months' <> (
                SELECT jsonb_agg(to_char(month_value, 'YYYY-MM-DD') ORDER BY month_value)
                FROM generate_series(NEW.coverage_start::timestamp, NEW.coverage_end::timestamp, interval '1 month') month_value
             ) THEN
            RAISE EXCEPTION 'archive manifest relational evidence mismatch';
          END IF;
          IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(NEW.manifest->'expected_trading_dates') value
                     WHERE value !~ '^\\d{4}-\\d{2}-\\d{2}$' OR value::date < NEW.coverage_start
                        OR value::date >= (NEW.coverage_end + interval '1 month')::date)
             OR EXISTS (SELECT 1 FROM (
                SELECT value, lag(value) OVER (ORDER BY ordinality) previous_value
                FROM jsonb_array_elements_text(NEW.manifest->'expected_trading_dates') WITH ORDINALITY dates(value, ordinality)
             ) ordered_dates WHERE previous_value IS NOT NULL AND value <= previous_value)
             OR EXISTS (SELECT 1 FROM generate_series(NEW.coverage_start::timestamp, NEW.coverage_end::timestamp, interval '1 month') month_value
                WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(NEW.manifest->'expected_trading_dates') date_value
                                  WHERE date_trunc('month', date_value::date) = month_value))
             OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.manifest->'instruments') value WHERE jsonb_typeof(value) <> 'string')
             OR (SELECT count(*) <> count(DISTINCT value) FROM jsonb_array_elements_text(NEW.manifest->'instruments') value)
             OR (SELECT encode(sha256(convert_to(string_agg(value, E'\\n' ORDER BY value), 'UTF8')), 'hex')
                 FROM jsonb_array_elements_text(NEW.manifest->'instruments') value) <> NEW.instruments_checksum
             OR (SELECT encode(sha256(convert_to(string_agg(value, E'\\n' ORDER BY ordinality), 'UTF8')), 'hex')
                 FROM jsonb_array_elements_text(NEW.manifest->'expected_trading_dates') WITH ORDINALITY dates(value, ordinality)) <> NEW.trading_dates_checksum THEN
            RAISE EXCEPTION 'archive continuity or checksum evidence is invalid';
          END IF;
          IF EXISTS (
            SELECT 1 FROM jsonb_array_elements(NEW.manifest->'chunks') value
            WHERE jsonb_typeof(value) <> 'object'
              OR (SELECT count(*) FROM jsonb_object_keys(value)) <> 6
              OR NOT value ?& ARRAY['snapshot_sequence','chunk_sequence','chunk_count','row_count','checksum_sha256','object']
              OR jsonb_typeof(value->'object') <> 'object'
              OR (SELECT count(*) FROM jsonb_object_keys(value->'object')) <> 3
              OR NOT (value->'object') ?& ARRAY['object_key','sha256','byte_size']
          ) OR EXISTS (
            SELECT 1 FROM jsonb_array_elements(NEW.manifest->'objects') value
            WHERE jsonb_typeof(value) <> 'object'
              OR (SELECT count(*) FROM jsonb_object_keys(value)) <> 3
              OR NOT value ?& ARRAY['object_key','sha256','byte_size']
          ) OR (
            SELECT count(*) <> count(DISTINCT value->>'object_key')
            FROM jsonb_array_elements(NEW.manifest->'objects') value
          ) THEN RAISE EXCEPTION 'archive manifest chunk evidence is malformed'; END IF;
          IF EXISTS (
            SELECT 1 FROM generate_series(NEW.coverage_start::timestamp, NEW.coverage_end::timestamp, interval '1 month') month_value
            WHERE NOT EXISTS (
              SELECT 1 FROM tw_minute_archive_chunk chunk
              WHERE chunk.release_id = NEW.release_id
                AND chunk.coverage_start <= month_value::date AND chunk.coverage_end >= month_value::date
            )
          ) THEN RAISE EXCEPTION 'archive chunks do not cover every release month'; END IF;
          IF (SELECT count(*) FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id) <> NEW.chunk_count
             OR (SELECT coalesce(sum(row_count), 0) FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id) <> NEW.row_count
             OR (SELECT min(coverage_start) FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id) <> NEW.coverage_start
             OR (SELECT max(coverage_end) FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id) <> NEW.coverage_end
             OR EXISTS (
               SELECT 1 FROM (
                 SELECT snapshot_sequence, count(*) AS actual_count,
                        min(chunk_sequence) AS first_chunk,
                        max(chunk_sequence) AS last_chunk,
                        min(chunk_count) AS declared_min,
                        max(chunk_count) AS declared_max
                 FROM tw_minute_archive_chunk
                 WHERE release_id = NEW.release_id
                 GROUP BY snapshot_sequence
               ) sequence_evidence
               WHERE actual_count <> declared_min
                  OR first_chunk <> 1
                  OR last_chunk <> declared_min
                  OR declared_min <> declared_max
             )
             OR (SELECT min(snapshot_sequence) FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id) <> 1
             OR (SELECT max(snapshot_sequence) FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id) <> NEW.sequence_count
             OR (SELECT count(DISTINCT snapshot_sequence) FROM tw_minute_archive_chunk WHERE release_id = NEW.release_id) <> NEW.sequence_count
             OR EXISTS (
               SELECT 1 FROM tw_minute_archive_chunk persisted
               WHERE persisted.release_id = NEW.release_id AND (
                 persisted.instrument_count <> NEW.instrument_count OR persisted.instrument_checksum <> NEW.instruments_checksum
                 OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.manifest->'chunks') item WHERE
                    item->>'snapshot_sequence' = persisted.snapshot_sequence::text AND item->>'chunk_sequence' = persisted.chunk_sequence::text
                    AND item->>'chunk_count' = persisted.chunk_count::text AND item->>'row_count' = persisted.row_count::text
                    AND item->>'checksum_sha256' = persisted.chunk_checksum AND item->'object'->>'object_key' = persisted.object_key
                    AND item->'object'->>'sha256' = persisted.object_checksum AND item->'object'->>'byte_size' = persisted.object_size_bytes::text)
               )
             ) OR EXISTS (
               SELECT 1 FROM tw_minute_archive_chunk persisted
               WHERE persisted.release_id = NEW.release_id
                 AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(NEW.manifest->'objects') item
                   WHERE item->>'object_key' = persisted.object_key
                     AND item->>'sha256' = persisted.object_checksum
                     AND item->>'byte_size' = persisted.object_size_bytes::text
                 )
             ) OR EXISTS (
               SELECT 1 FROM jsonb_array_elements(NEW.manifest->'chunks') item
               WHERE NOT EXISTS (SELECT 1 FROM tw_minute_archive_chunk persisted WHERE persisted.release_id = NEW.release_id
                 AND item->>'snapshot_sequence' = persisted.snapshot_sequence::text AND item->>'chunk_sequence' = persisted.chunk_sequence::text
                 AND item->>'chunk_count' = persisted.chunk_count::text AND item->>'row_count' = persisted.row_count::text
                 AND item->>'checksum_sha256' = persisted.chunk_checksum AND item->'object'->>'object_key' = persisted.object_key
                 AND item->'object'->>'sha256' = persisted.object_checksum AND item->'object'->>'byte_size' = persisted.object_size_bytes::text)
             ) THEN RAISE EXCEPTION 'archive chunks do not match manifest evidence'; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_tw_minute_publication_warning() RETURNS trigger AS $$
        BEGIN
          IF NEW.status <> 'completed_with_warnings' THEN RETURN NEW; END IF;
          IF (
               SELECT count(DISTINCT snapshot.dataset_key)
               FROM tw_minute_dataset_snapshot snapshot
               WHERE snapshot.daily_update_id = NEW.daily_update_id
                 AND snapshot.dataset_key IN ('tw_equity_minute', 'tw_etf_minute')
             ) <> 2
             OR EXISTS (
               SELECT 1
               FROM tw_minute_dataset_snapshot snapshot
               WHERE snapshot.daily_update_id = NEW.daily_update_id
                 AND snapshot.dataset_key IN ('tw_equity_minute', 'tw_etf_minute')
                 AND (
                   snapshot.status <> 'completed'
                   OR jsonb_typeof(snapshot.symbol_results) <> 'object'
                   OR snapshot.symbol_results = '{}'::jsonb
                 )
             )
             OR EXISTS (
               SELECT 1
               FROM tw_minute_dataset_snapshot snapshot
               CROSS JOIN LATERAL jsonb_each(snapshot.symbol_results) entry
               WHERE snapshot.daily_update_id = NEW.daily_update_id
                 AND snapshot.dataset_key IN ('tw_equity_minute', 'tw_etf_minute')
                 AND (
                   btrim(entry.key) = ''
                   OR CASE jsonb_typeof(entry.value)
                     WHEN 'string' THEN entry.value #>> '{}' NOT IN ('data', 'expected_no_data')
                     WHEN 'object' THEN
                       coalesce(entry.value->>'outcome', entry.value->>'status', '')
                         NOT IN ('data', 'expected_no_data')
                       OR (
                         entry.value ? 'outcome'
                         AND entry.value ? 'status'
                         AND entry.value->>'outcome' IS DISTINCT FROM entry.value->>'status'
                       )
                     ELSE TRUE
                   END
                 )
             )
             OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.manifest->'expected_no_data_symbols') item
                     WHERE jsonb_typeof(item) <> 'string' OR btrim(item #>> '{}') = '')
             OR (SELECT count(*) <> count(DISTINCT item #>> '{}')
                 FROM jsonb_array_elements(NEW.manifest->'expected_no_data_symbols') item)
             OR (SELECT array_agg(item #>> '{}' ORDER BY item #>> '{}') FROM jsonb_array_elements(NEW.manifest->'expected_no_data_symbols') item)
                IS DISTINCT FROM
                (SELECT array_agg(symbol ORDER BY symbol) FROM (
                   SELECT DISTINCT entry.key AS symbol FROM tw_minute_dataset_snapshot snapshot
                   CROSS JOIN LATERAL jsonb_each(snapshot.symbol_results) entry
                   WHERE snapshot.daily_update_id = NEW.daily_update_id
                     AND snapshot.dataset_key IN ('tw_equity_minute', 'tw_etf_minute')
                     AND ((jsonb_typeof(entry.value) = 'string' AND entry.value #>> '{}' = 'expected_no_data')
                       OR (jsonb_typeof(entry.value) = 'object' AND (entry.value->>'status' = 'expected_no_data' OR entry.value->>'outcome' = 'expected_no_data')))
                ) expected) THEN
            RAISE EXCEPTION 'expected_no_data manifest evidence does not match snapshots';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """CREATE TRIGGER tw_minute_publication_warning_guard BEFORE INSERT OR UPDATE ON tw_minute_publication_revision FOR EACH ROW EXECUTE FUNCTION validate_tw_minute_publication_warning()"""
    )
    op.execute(
        """
        CREATE FUNCTION validate_tw_minute_universe_publication() RETURNS trigger AS $$
        DECLARE actual_count integer; actual_checksum text;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.status IN ('published', 'superseded') THEN
              RAISE EXCEPTION 'published universe release is immutable';
            END IF;
            RETURN OLD;
          END IF;
          IF TG_OP = 'UPDATE' AND OLD.status IN ('published', 'superseded') THEN
            IF OLD.status = 'published'
               AND NEW.status = 'superseded'
               AND (to_jsonb(NEW) - 'status' - 'updated_at')
                   = (to_jsonb(OLD) - 'status' - 'updated_at') THEN
              RETURN NEW;
            END IF;
            RAISE EXCEPTION 'published universe release is immutable';
          END IF;
          IF NEW.status <> 'published' THEN RETURN NEW; END IF;
          SELECT count(*), encode(sha256(convert_to(coalesce(string_agg(
            instrument_id::text || '|' || symbol || '|' || started_on::text || '|' || coalesce(ended_on::text, '') || '|' || coalesce(source_symbol, ''),
            E'\\n' ORDER BY instrument_id::text, symbol, started_on, ended_on, source_symbol), ''), 'UTF8')), 'hex')
          INTO actual_count, actual_checksum FROM tw_minute_universe_member WHERE release_id = NEW.release_id;
          IF actual_count = 0 OR NEW.member_count <> actual_count OR NEW.member_checksum <> actual_checksum
             OR NEW.shioaji_eligibility_checksum <> encode(sha256(convert_to(NEW.shioaji_eligibility_snapshot::text, 'UTF8')), 'hex')
             OR NEW.official_membership_checksum <> encode(sha256(convert_to(NEW.official_membership_snapshot::text, 'UTF8')), 'hex') THEN
            RAISE EXCEPTION 'published universe evidence does not match members or snapshots';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """CREATE TRIGGER tw_minute_universe_publication_guard BEFORE INSERT OR UPDATE OR DELETE ON tw_minute_universe_release FOR EACH ROW EXECUTE FUNCTION validate_tw_minute_universe_publication()"""
    )
    op.execute(
        """
        CREATE FUNCTION guard_tw_minute_published_universe_members() RETURNS trigger AS $$
        DECLARE target_release uuid := COALESCE(NEW.release_id, OLD.release_id);
        BEGIN
          IF EXISTS (SELECT 1 FROM tw_minute_universe_release
                     WHERE release_id IN (COALESCE(NEW.release_id, OLD.release_id), OLD.release_id)
                       AND status IN ('published', 'superseded')) THEN
            RAISE EXCEPTION 'published universe members are immutable';
          END IF;
          IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """CREATE TRIGGER tw_minute_universe_member_immutability_guard BEFORE INSERT OR UPDATE OR DELETE ON tw_minute_universe_member FOR EACH ROW EXECUTE FUNCTION guard_tw_minute_published_universe_members()"""
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS tw_minute_universe_member_immutability_guard ON tw_minute_universe_member"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_tw_minute_published_universe_members()")
    op.execute(
        "DROP TRIGGER IF EXISTS tw_minute_universe_publication_guard ON tw_minute_universe_release"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_tw_minute_universe_publication()")
    op.execute(
        "DROP TRIGGER IF EXISTS tw_minute_publication_warning_guard ON tw_minute_publication_revision"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_tw_minute_publication_warning()")
    op.execute(
        "DROP TRIGGER IF EXISTS tw_minute_archive_chunk_immutability_guard ON tw_minute_archive_chunk"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_tw_minute_finalized_archive_chunks()")
    op.execute(
        "DROP TRIGGER IF EXISTS tw_minute_archive_finalization_guard ON tw_minute_archive_release"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_tw_minute_archive_finalization()")
    op.drop_table("tw_minute_archive_chunk")
    op.drop_table("tw_minute_archive_release")
    op.drop_index("uq_tw_minute_publication_latest", table_name="tw_minute_publication_revision")
    op.drop_table("tw_minute_publication_revision")
    op.drop_table("tw_minute_snapshot_part")
    op.drop_index("idx_tw_minute_snapshot_update", table_name="tw_minute_dataset_snapshot")
    op.drop_table("tw_minute_dataset_snapshot")
    op.drop_index("idx_tw_minute_daily_update_status", table_name="tw_minute_daily_update")
    op.drop_table("tw_minute_daily_update")
    op.drop_index(
        "idx_tw_minute_universe_member_instrument", table_name="tw_minute_universe_member"
    )
    op.drop_table("tw_minute_universe_member")
    op.drop_index("idx_tw_minute_universe_release_lookup", table_name="tw_minute_universe_release")
    op.drop_table("tw_minute_universe_release")
    op.drop_table("market_data_minute")
