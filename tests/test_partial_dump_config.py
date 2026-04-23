from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.schemas.partial_dump import load_partial_dump_config


def _base_config() -> dict:
    return {
        "version": "1",
        "profile": "test-profile",
        "source": {
            "database_url_env": "FINDB_REMOTE_DATABASE_URL",
            "require_ssl": True,
            "statement_timeout_ms": 120000,
            "application_name": "findb-partial-dump",
            "readonly_required": True,
        },
        "target": {
            "output_dir": "seed/partial_dump",
            "artifact_name": "{profile}_{utc_ts}",
            "format": "csv",
            "compress": "zstd",
            "include_schema_sql": True,
            "include_load_sql": True,
            "include_manifest": True,
        },
        "snapshot": {
            "enabled": True,
            "isolation_level": "repeatable_read",
            "lock_timeout_ms": 5000,
            "idle_in_transaction_timeout_ms": 60000,
        },
        "defaults": {
            "mode": "where",
            "where_sql": None,
            "params": {},
            "order_by": None,
            "limit": None,
            "chunk_size": 50000,
        },
        "selection": {
            "window_days": 365,
            "instrument_sample_size": 3,
            "instrument_scope": "by_market",
            "non_instrument_table_strategy": "time_window",
            "time_columns_priority": ["trade_date", "obs_date", "created_at", "updated_at"],
            "unresolved_table_policy": "discuss",
        },
        "table_discovery": {
            "enabled": True,
            "include_schemas": ["public", "raw"],
            "exclude_tables": [],
            "require_pk_for_upsert": True,
        },
        "tables": [
            {
                "schema": "raw",
                "name": "market_payload",
                "mode": "latest_n_days",
                "latest_by_column": "fetched_at",
                "latest_n_days": 365,
                "limit": 1000,
            }
        ],
        "anonymization_rules": {},
        "load": {
            "truncate_before_load": True,
            "disable_triggers": False,
            "verify_fk_after_load": True,
            "on_conflict": "upsert",
            "parallel_jobs": 1,
        },
        "validation": {
            "row_count_tolerance_pct": 0.0,
            "required_tables": [],
            "table_checks": [],
        },
        "runtime": {
            "retries": 2,
            "retry_backoff_ms": 1000,
            "log_level": "info",
            "fail_fast": True,
        },
        "metadata": {
            "owner": "data-platform",
            "ticket": "FINDB-PartialDump-Prod",
            "tags": ["prod", "partial-dump"],
            "notes": None,
        },
    }


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_load_partial_dump_config_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "partial_dump.yaml"
    _write_yaml(config_file, _base_config())
    monkeypatch.setenv(
        "FINDB_REMOTE_DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/db"
    )

    config = load_partial_dump_config(config_file)

    assert config.version == "1"
    assert config.selection.window_days == 365
    assert config.tables[0].limit == 1000


def test_window_days_must_be_365_or_less(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _base_config()
    payload["selection"]["window_days"] = 366
    config_file = tmp_path / "partial_dump.yaml"
    _write_yaml(config_file, payload)
    monkeypatch.setenv(
        "FINDB_REMOTE_DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/db"
    )

    with pytest.raises(ValidationError):
        load_partial_dump_config(config_file)


def test_raw_market_payload_requires_explicit_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _base_config()
    payload["tables"][0].pop("limit")
    config_file = tmp_path / "partial_dump.yaml"
    _write_yaml(config_file, payload)
    monkeypatch.setenv(
        "FINDB_REMOTE_DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/db"
    )

    with pytest.raises(ValidationError):
        load_partial_dump_config(config_file)


def test_raw_market_payload_limit_must_be_1000_or_less(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _base_config()
    payload["tables"][0]["limit"] = 1001
    config_file = tmp_path / "partial_dump.yaml"
    _write_yaml(config_file, payload)
    monkeypatch.setenv(
        "FINDB_REMOTE_DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/db"
    )

    with pytest.raises(ValidationError):
        load_partial_dump_config(config_file)


def test_discovery_including_raw_requires_market_payload_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _base_config()
    payload["tables"] = []
    config_file = tmp_path / "partial_dump.yaml"
    _write_yaml(config_file, payload)
    monkeypatch.setenv(
        "FINDB_REMOTE_DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/db"
    )

    with pytest.raises(ValidationError):
        load_partial_dump_config(config_file)


def test_columns_include_and_exclude_cannot_both_be_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _base_config()
    payload["tables"][0]["columns_include"] = ["id"]
    payload["tables"][0]["columns_exclude"] = ["payload"]
    config_file = tmp_path / "partial_dump.yaml"
    _write_yaml(config_file, payload)
    monkeypatch.setenv(
        "FINDB_REMOTE_DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/db"
    )

    with pytest.raises(ValidationError):
        load_partial_dump_config(config_file)


def test_missing_source_env_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "partial_dump.yaml"
    _write_yaml(config_file, _base_config())
    monkeypatch.delenv("FINDB_REMOTE_DATABASE_URL", raising=False)

    with pytest.raises(ValueError):
        load_partial_dump_config(config_file)
