"""Static guardrails for the staging four-feed cutover migration."""

from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "d4e5f6a7b8c9_stage_four_feeds.py"
)


def test_stage_four_migration_is_linear_and_lossy_by_design() -> None:
    source = MIGRATION.read_text()
    assert 'revision: str = "d4e5f6a7b8c9"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"' in source
    assert "Downgrade is lossy" in source
    assert "DELETE FROM dataset_registry WHERE dataset_key NOT IN" in source


def test_stage_four_migration_preserves_supported_data_and_fails_closed_on_legacy_rows() -> None:
    source = MIGRATION.read_text()
    guard = source.split("def _update_contract_configs", 1)[0]
    assert "def _assert_no_obsolete_dataset_rows" in guard
    assert "market_data_eod" not in guard
    assert "futures_continuous_eod" not in guard
    assert "normalization_outbox" in guard
    assert "missing_delivery_alert" in guard
    assert "ELSE '[]'::jsonb" in source
    assert "AND allowed_datasets IS NULL THEN" in source
    assert "DELETE FROM scheduler_control" in source
    assert "scheduler_key NOT IN" in source
    assert "'[\"us_equity_eod\"]'::jsonb" in source
    assert '\'["tw_equity_minute","tw_etf_minute"]\'::jsonb' in source
