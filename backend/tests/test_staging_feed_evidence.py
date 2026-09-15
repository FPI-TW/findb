"""Tests for the staging active-feed evidence probes and coordinator."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_minute_lineage_explicitly_marks_serve_not_applicable() -> None:
    module = _load(
        "export_staging_feed_evidence",
        REPO_ROOT / "backend" / "scripts" / "export_staging_feed_evidence.py",
    )

    lineage = module._lineage_contract("market_minute")

    assert lineage["serve"] == "not_applicable"
    assert lineage["serve_reason"] == "market_minute_read_model_not_exposed"
    assert lineage["operational_read_paths"] == [
        "admin_raw",
        "admin_market_freshness",
        "dashboard",
    ]


def test_assessment_requires_persistent_202_and_two_dates() -> None:
    module = _load(
        "collect_staging_feed_evidence",
        REPO_ROOT / "infra" / "acceptance" / "collect_staging_feed_evidence.py",
    )
    feeds = []
    for source, dataset, schema in (
        ("twelve_data", "us_equity_eod", "market_eod"),
        ("finlab", "tw_equity_eod", "market_eod"),
        ("shioaji", "tw_equity_minute", "market_minute"),
        ("shioaji", "tw_etf_minute", "market_minute"),
    ):
        feeds.append(
            {
                "source": source,
                "dataset_key": dataset,
                "multi_trade_date_ready": True,
                "lineage_contract": {
                    "serve": "not_applicable" if schema == "market_minute" else "required"
                },
                "runs": [
                    {
                        "source_http_status": 202,
                        "attempt_status": "accepted",
                    }
                ],
            }
        )
    fetcher = {
        provider: {
            "provider": provider,
            "recent_trade_dates": [
                {"target_data_date": "2026-09-08"},
                {"target_data_date": "2026-09-05"},
            ],
        }
        for provider in ("twelve_data", "finlab")
    }
    fetcher["shioaji"] = {
        "provider": "shioaji",
        "recent_trade_dates": [
            {"target_date": "2026-09-08"},
            {"target_date": "2026-09-05"},
        ],
    }

    assessment = module._assessment({"feeds": feeds}, fetcher)

    assert assessment["four_feed_scope_complete"] is True
    assert assessment["backend_multi_trade_date_complete"] is True
    assert assessment["fetcher_multi_trade_date_complete"] is True
    assert assessment["persistent_source_202"] == {
        "finlab/tw_equity_eod": True,
        "twelve_data/us_equity_eod": True,
    }
    assert assessment["minute_serve_boundary_complete"] is True


def test_daily_fetcher_probe_exports_hashes_credit_budget_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load(
        "export_fetcher_feed_evidence",
        REPO_ROOT / "infra" / "acceptance" / "export_fetcher_feed_evidence.py",
    )
    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "universe_id": "reviewed_v1",
                "credit_cost_per_symbol": 1,
                "symbols": [
                    {"symbol": "A", "canonical_symbol": "A"},
                    {"symbol": "B", "canonical_symbol": "B"},
                ],
            }
        ),
        encoding="utf-8",
    )
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(
        json.dumps(
            {
                "timezone": "Asia/Taipei",
                "feeds": [
                    {
                        "provider": "twelve_data",
                        "dataset_key": "us_equity_eod",
                        "universe_file": universe_path.name,
                        "slot_id": "western",
                        "scheduled_time": "08:15",
                        "target_date_policy": "latest_trade_date",
                        "max_credits_per_run": 5,
                        "max_records_per_run": 100,
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "state.sqlite3"
    with sqlite3.connect(state_path) as db:
        db.execute(
            """CREATE TABLE scheduled_job (
                provider TEXT, dataset_key TEXT, target_data_date TEXT,
                scheduled_date TEXT, status TEXT, last_outcome TEXT,
                attempt_count INTEGER, record_count INTEGER,
                attempt_id TEXT, run_id TEXT, checkpoint_before TEXT,
                checkpoint_after TEXT
            )"""
        )
        db.executemany(
            "INSERT INTO scheduled_job VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "twelve_data",
                    "us_equity_eod",
                    date,
                    date,
                    "completed",
                    "success",
                    1,
                    2,
                    "a",
                    "r",
                    "2026-09-01",
                    date,
                )
                for date in ("2026-09-08", "2026-09-05")
            ],
        )
    monkeypatch.setenv("FETCHER_SCHEDULE_FILE", str(schedule_path))
    monkeypatch.setenv("FETCHER_STATE_PATH", str(state_path))

    result = module._daily_provider("twelve_data")

    assert result["credits"]["estimated_per_run"] == 2
    assert result["credits"]["configured_cap"] == 5
    assert len(result["config"]["schedule_sha256"]) == 64
    assert len(result["config"]["universe_sha256"]) == 64
    assert {row["target_data_date"] for row in result["recent_trade_dates"]} == {
        "2026-09-08",
        "2026-09-05",
    }


def test_calibration_identity_is_bounded_and_unique() -> None:
    module = _load(
        "calibrate_staging_provider_alerts",
        REPO_ROOT / "infra" / "acceptance" / "calibrate_staging_provider_alerts.py",
    )
    request = {
        "request_key": "r" * 100,
        "idempotency_key": "i" * 100,
        "fetched_at": "2026-01-01T00:00:00Z",
    }

    module._identity(request, "missing-close")

    assert len(request["request_key"]) <= 100
    assert len(request["idempotency_key"]) <= 100
    assert "staging-cal-missing-close" in request["request_key"]
    assert request["fetched_at"] != "2026-01-01T00:00:00Z"
