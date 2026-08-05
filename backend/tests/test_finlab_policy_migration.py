"""Focused regression checks for the FinLab pilot policy migration."""

import importlib.util
from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "e3f4a5b6c7d8_finlab_pilot_policy_override.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("finlab_policy_migration", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load policy migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_chained_after_scheduler_definition_and_has_safe_guards(monkeypatch):
    migration = _load_migration()
    assert migration.down_revision == "d2e3f4a5b6c7"

    statements: list[str] = []
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement, *params: statements.append(str(statement)),
    )
    migration.upgrade()
    assert len(statements) == 1
    upgrade_sql = statements[0]
    assert "dataset_key = 'tw_equity_eod'" in upgrade_sql
    assert "NOT (config->'delivery_expectation' ? 'source_overrides')" in upgrade_sql
    assert "NOT (config->'delivery_expectation'->'source_overrides' ? 'finlab')" in upgrade_sql
    assert "CAST(:finlab_override AS jsonb)" in upgrade_sql

    statements.clear()
    migration.downgrade()
    assert len(statements) == 1
    downgrade_sql = statements[0]
    assert "= CAST(:finlab_override AS jsonb)" in downgrade_sql
    assert "- 'finlab'" in downgrade_sql
