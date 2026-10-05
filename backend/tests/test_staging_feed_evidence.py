"""Tests for the staging active-feed evidence probes and coordinator."""

from __future__ import annotations

import ast
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import random
import shlex
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
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


def _sample_record(schema, *, provider="shioaji", run_id="run"):
    record = {
        "instrument_id": "0199aabb-ccdd-7000-8000-000000000001",
        "trade_date": "2026-10-02",
        "source": provider,
        "source_fetched_at": "2026-10-02T10:00:00+00:00",
        "run_id": run_id,
        "open": "100.10000000",
        "high": "102",
        "low": "99",
        "close": "101",
        "volume": 10,
    }
    record.update(
        {
            "market_eod": {"total_ticks": None, "turnover": "1001.0000"},
            "market_minute": {
                "bar_start_time": "2026-10-02T01:30:00Z",
                "bar_end_time": "2026-10-02T01:31:00Z",
                "signal_time": "2026-10-02T01:31:00Z",
                "market_timezone": "Asia/Taipei",
                "turnover": "1001",
                "trade_count": None,
                "price_adjustment": "none",
            },
            "futures_eod": {
                "contract_id": "contract",
                "product_code": "TX",
                "contract_code": "TX:202610",
                "contract_month": "202610",
                "session": "regular",
                "settlement_price": "101",
                "open_interest": 20,
            },
        }[schema]
    )
    return record


def _pilot_package():
    module = _load(
        "pilot_acceptance", REPO_ROOT / "infra/acceptance/collect_staging_feed_evidence.py"
    )
    backend_probe = _load(
        "pilot_backend", REPO_ROOT / "backend/scripts/export_staging_feed_evidence.py"
    )
    assert {(source, dataset) for source, dataset, _schema in backend_probe.ACTIVE_FEEDS} == set(
        module.catalog_feeds()
    )
    feeds = []
    fetcher = {}
    symbols = {"twelve_data": ["AAPL", "MSFT"], "finlab": ["2330", "2317"], "taifex": ["TX", "MTX"]}
    for provider, dataset in module.catalog_feeds():
        run_id = provider + ":" + dataset
        records = []
        for symbol in symbols.get(provider, ["2330" if dataset == "tw_equity_minute" else "0050"]):
            for session in ["regular", "after_hours"] if provider == "taifex" else [None]:
                records.append(
                    {
                        "symbol": symbol,
                        "trade_date": "2026-10-02",
                        "row_count": 1,
                        **(
                            {
                                "session": session,
                                "contract_month": "202610",
                                "contract_code": symbol + ":202610",
                            }
                            if session
                            else {}
                        ),
                    }
                )
        feeds.append(
            {
                "source": provider,
                "dataset_key": dataset,
                "lineage_contract": {"serve": "required"},
                "multi_trade_date_ready": False,
                "serve_probe": {"success": True, "trade_date": "2026-10-02", "run_id": run_id},
                "runs": [
                    {
                        "run_id": run_id,
                        "batch_data_date": "2026-10-02",
                        "source_http_status": 202,
                        "attempt_status": "accepted",
                        "raw_persisted": True,
                        "status": "completed",
                        "job_status": "completed",
                        "dq_errors": 0,
                        "canonical_rows": len(records),
                        "success_records": len(records),
                        "total_records": len(records),
                        "canonical_coverage": records,
                        "attempt_receipts": [
                            {
                                "attempt_id": run_id + ":accepted",
                                "run_id": run_id,
                                "source": provider,
                                "dataset_key": dataset,
                                "schema_id": next(
                                    item[2]
                                    for item in backend_probe.ACTIVE_FEEDS
                                    if item[:2] == (provider, dataset)
                                ),
                                "schema_version": 1,
                                "http_status": 202,
                                "status": "accepted",
                                "request_sha256": "a" * 64,
                            }
                        ],
                    }
                ],
            }
        )
        schema = next(
            item[2] for item in backend_probe.ACTIVE_FEEDS if item[:2] == (provider, dataset)
        )
        sample_record = _sample_record(schema, provider=provider, run_id=run_id)
        sample = {
            "sample_instrument_id": sample_record["instrument_id"],
            "sample_trade_date": sample_record["trade_date"],
            "sample_run_id": run_id,
            "sample_record": sample_record,
        }
        projection = backend_probe._canonical_projection(schema, sample_record)
        fingerprint = backend_probe._projection_fingerprint(projection)
        feeds[-1].update(schema_id=schema, canonical_sample=sample)
        feeds[-1]["serve_probe"].update(
            http_status=200,
            instrument_id=sample_record["instrument_id"],
            projection_version=1,
            canonical_projection=projection,
            returned_projection=projection,
            canonical_fingerprint=fingerprint,
            returned_fingerprint=fingerprint,
        )
        feeds[-1]["runs"][0].update(
            job_id=run_id + ":job",
            job_delivery_id=run_id + ":delivery",
            outbox_status="published",
            outbox_receipts=[
                {
                    "outbox_id": run_id + ":outbox",
                    "job_id": run_id + ":job",
                    "run_id": run_id,
                    "delivery_id": run_id + ":delivery",
                    "status": "published",
                    "event_type": "normalize_run",
                }
            ],
        )
        fetcher.setdefault(provider, {"provider": provider, "recent_trade_dates": []})[
            "recent_trade_dates"
        ].append(
            {
                "dataset_key": dataset,
                "target_date": "2026-10-02",
                "status": "completed",
                "run_id": run_id,
            }
        )
    catalog = json.loads(
        (REPO_ROOT / "fetcher/configs/staging_provider_pilots.v1.json").read_bytes()
    )
    for provider, filename in {
        "twelve_data": "twelve_data_us_staging_pilot.v2.json",
        "finlab": "finlab_tw_review_required.v1.json",
        "shioaji": "shioaji_tw_staging_pilot.v3.json",
        "taifex": "taifex_tw_staging_pilot.v1.json",
    }.items():
        fetcher[provider]["config"] = {
            "universe_sha256"
            if provider in {"twelve_data", "finlab"}
            else "manifest_sha256": catalog["files"][filename],
            "schedule_sha256": catalog["files"]["daily_scheduler.staging.v3.json"],
        }
    observed_at = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
    return (
        module,
        {"feeds": feeds, "target_calendar_evidence": _calendar_evidence(observed_at)},
        fetcher,
        observed_at,
    )


def _calendar_evidence(observed_at: datetime) -> dict:
    today = (observed_at + timedelta(hours=8)).date()
    markets = {}
    for market in ("US", "TW", "TAIFEX"):
        timezone_name = "America/New_York" if market == "US" else "Asia/Taipei"
        markets[market] = {
            "valid": True,
            "timezone": timezone_name,
            "revisions": [
                {
                    "year": year,
                    "revision": 1,
                    "revision_id": f"{market}:{year}:1",
                    "status": "published",
                    "coverage_complete": True,
                    "timezone": timezone_name,
                }
                for year in {(today - timedelta(days=offset)).year for offset in range(16)}
            ],
            "days": [
                {
                    "trade_date": (today - timedelta(days=offset)).isoformat(),
                    "day_status": "open"
                    if (today - timedelta(days=offset)).weekday() < 5
                    else "closed",
                    "is_open": (today - timedelta(days=offset)).weekday() < 5,
                    "revision": 1,
                    "revision_id": f"{market}:{(today - timedelta(days=offset)).year}:1",
                    "session_close": None,
                }
                for offset in range(16)
            ],
        }
    return {"schema_version": 1, "observed_at_utc": observed_at.isoformat(), "markets": markets}


def test_one_completed_day_all_five_pilots_requires_exact_scope_and_run_lineage() -> None:
    module, findb, fetcher, observed_at = _pilot_package()
    feeds = findb["feeds"]
    assessment = module._assessment(findb, fetcher, observed_at=observed_at)
    assert assessment["pilot_functional_complete"] is True
    assert assessment["serve_boundary_complete"] is True
    assert len(assessment["pilot_functional_acceptance"]) == 5
    assert assessment["backend_multi_trade_date_complete"] is False
    unbound = deepcopy(fetcher)
    unbound["twelve_data"]["config"]["universe_sha256"] = "old-config"
    assert (
        module._assessment(findb, unbound, observed_at=observed_at)["pilot_functional_complete"]
        is False
    )
    for mutation in ("old_run", "wrong_symbol", "missing_session", "split_expiry"):
        altered = deepcopy(feeds)
        if mutation == "old_run":
            altered[0]["serve_probe"]["run_id"] = "unrelated-legacy-run"
        elif mutation == "wrong_symbol":
            altered[0]["runs"][0]["canonical_coverage"][0]["symbol"] = "NVDA"
        elif mutation == "missing_session":
            altered[-1]["runs"][0]["canonical_coverage"].pop()
        else:
            record = altered[-1]["runs"][0]["canonical_coverage"][0]
            record.update(contract_month="202611", contract_code=record["symbol"] + ":202611")
        assert (
            module._assessment({**findb, "feeds": altered}, fetcher, observed_at=observed_at)[
                "pilot_functional_complete"
            ]
            is False
        ), mutation


def test_minute_lineage_requires_the_serve_boundary() -> None:
    module = _load(
        "export_staging_feed_evidence",
        REPO_ROOT / "backend" / "scripts" / "export_staging_feed_evidence.py",
    )

    lineage = module._lineage_contract("market_minute")

    assert lineage["serve"] == "required"
    assert lineage["serve_reason"] is None
    assert lineage["operational_read_paths"] == [
        "serve",
        "admin_market_freshness",
        "dashboard",
    ]


def test_serve_probe_fails_closed_without_static_cache_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "export_staging_feed_evidence_missing_key",
        REPO_ROOT / "backend" / "scripts" / "export_staging_feed_evidence.py",
    )
    monkeypatch.delenv("FINDB_STATIC_CACHE_SERVE_API_KEY", raising=False)

    probe = module._serve_probe(
        "market_eod",
        {
            "sample_instrument_id": "0199aabb-ccdd-7000-8000-000000000001",
            "sample_trade_date": "2026-09-18",
        },
    )

    assert probe == {
        "success": False,
        "http_status": None,
        "reason": "serve_credential_missing",
    }


def test_serve_samples_are_selected_from_pilot_canonical_scope_with_run_lineage() -> None:
    module = _load(
        "export_staging_feed_evidence_samples",
        REPO_ROOT / "backend" / "scripts" / "export_staging_feed_evidence.py",
    )

    sql = str(module.SAMPLES)

    assert "sample_run_id" in sql
    assert "AAPL" in sql and "MSFT" in sql
    assert "2330" in sql and "0050" in sql
    assert "market_data_eod" in sql
    assert "market_data_minute" in sql
    assert "i.market = 'US' AND i.asset_class = 'equity'" in sql
    assert "i.market = 'TW' AND i.asset_class = 'equity'" in sql
    assert "i.market = 'TW' AND i.asset_class = 'etf'" in sql


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
        ("taifex", "tw_futures_eod", "futures_eod"),
    ):
        feeds.append(
            {
                "source": source,
                "dataset_key": dataset,
                "multi_trade_date_ready": True,
                "lineage_contract": {"serve": "required"},
                "serve_probe": {"success": True},
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

    fetcher["taifex"] = {
        "provider": "taifex",
        "recent_trade_dates": [{"target_date": "2026-09-08"}, {"target_date": "2026-09-05"}],
    }
    assessment = module._assessment({"feeds": feeds}, fetcher)

    assert assessment["five_feed_scope_complete"] is True
    assert assessment["backend_multi_trade_date_complete"] is True
    assert assessment["fetcher_multi_trade_date_complete"] is True
    assert assessment["persistent_source_202"] == {
        "finlab/tw_equity_eod": True,
        "twelve_data/us_equity_eod": True,
        "shioaji/tw_equity_minute": True,
        "shioaji/tw_etf_minute": True,
        "taifex/tw_futures_eod": True,
    }
    # An unbound success label cannot prove the HTTP response's provenance.
    assert assessment["serve_boundary_complete"] is False
    assert set(assessment["serve_probe"].values()) == {False}


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


@pytest.mark.parametrize("status", ["blocked", "failed", "pending", None])
def test_old_success_cannot_hide_latest_eligible_missing_or_blocked_day(status) -> None:
    module, findb, fetcher, _ = _pilot_package()
    observed_at = datetime(2026, 10, 5, 10, tzinfo=timezone.utc)
    findb["target_calendar_evidence"] = _calendar_evidence(observed_at)
    if status:
        for provider in ("finlab", "shioaji", "taifex"):
            for dataset in fetcher[provider].get("datasets", []) or [
                row["dataset_key"] for row in fetcher[provider]["recent_trade_dates"]
            ]:
                fetcher[provider]["recent_trade_dates"].append(
                    {
                        "dataset_key": dataset,
                        "target_date": "2026-10-05",
                        "status": status,
                    }
                )
    acceptance = module._functional_acceptance(findb, fetcher, observed_at=observed_at)
    assert acceptance["twelve_data/us_equity_eod"] is True
    assert all(not value for key, value in acceptance.items() if not key.startswith("twelve_data/"))


@pytest.mark.parametrize(
    "utc_time,expected",
    [
        (
            "2026-10-06T00:14:00+00:00",
            {
                "twelve_data": "2026-10-02",
                "finlab": "2026-10-05",
                "shioaji": "2026-10-05",
                "taifex": "2026-10-05",
            },
        ),
        (
            "2026-10-06T00:15:00+00:00",
            {
                "twelve_data": "2026-10-05",
                "finlab": "2026-10-05",
                "shioaji": "2026-10-05",
                "taifex": "2026-10-05",
            },
        ),
        (
            "2026-10-06T00:30:00+00:00",
            {
                "twelve_data": "2026-10-05",
                "finlab": "2026-10-05",
                "shioaji": "2026-10-05",
                "taifex": "2026-10-05",
            },
        ),
        (
            "2026-10-06T01:00:00+00:00",
            {
                "twelve_data": "2026-10-05",
                "finlab": "2026-10-05",
                "shioaji": "2026-10-05",
                "taifex": "2026-10-05",
            },
        ),
        (
            "2026-10-06T06:30:00+00:00",
            {
                "twelve_data": "2026-10-05",
                "finlab": "2026-10-06",
                "shioaji": "2026-10-06",
                "taifex": "2026-10-05",
            },
        ),
        (
            "2026-10-06T09:00:00+00:00",
            {
                "twelve_data": "2026-10-05",
                "finlab": "2026-10-06",
                "shioaji": "2026-10-06",
                "taifex": "2026-10-05",
            },
        ),
        (
            "2026-10-06T10:00:00+00:00",
            {
                "twelve_data": "2026-10-05",
                "finlab": "2026-10-06",
                "shioaji": "2026-10-06",
                "taifex": "2026-10-06",
            },
        ),
    ],
)
def test_targets_follow_runtime_due_times_and_market_calendar(utc_time, expected) -> None:
    module, findb, _, _ = _pilot_package()
    observed_at = datetime.fromisoformat(utc_time)
    findb["target_calendar_evidence"] = _calendar_evidence(observed_at)
    assert {
        provider: module._expected_target(findb, provider, observed_at=observed_at)
        for provider in expected
    } == expected


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "revision",
        "incomplete",
        "unpublished",
        "stale_clock",
        "naive_clock",
        "timezone",
        "missing_day",
    ],
)
def test_calendar_evidence_fails_closed(mutation) -> None:
    module, findb, fetcher, observed_at = _pilot_package()
    evidence = findb["target_calendar_evidence"]
    if mutation == "missing":
        findb.pop("target_calendar_evidence")
    elif mutation == "stale_clock":
        evidence["observed_at_utc"] = (observed_at - timedelta(minutes=6)).isoformat()
    elif mutation == "naive_clock":
        evidence["observed_at_utc"] = observed_at.replace(tzinfo=None).isoformat()
    else:
        calendar = evidence["markets"]["US"]
        if mutation == "revision":
            for day in calendar["days"]:
                day["revision"] = 2
        elif mutation == "missing_day":
            calendar["days"] = [
                day for day in calendar["days"] if day["trade_date"] != "2026-10-02"
            ]
        elif mutation == "incomplete":
            calendar["revisions"][0]["coverage_complete"] = False
        elif mutation == "unpublished":
            calendar["revisions"][0]["status"] = "draft"
        else:
            calendar["timezone"] = "Asia/Taipei"
    assert (
        module._assessment(findb, fetcher, observed_at=observed_at)["pilot_functional_complete"]
        is False
    )


def test_official_closed_day_uses_previous_open_date_and_close_time() -> None:
    module, findb, _, _ = _pilot_package()
    observed_at = datetime(2026, 10, 5, 10, tzinfo=timezone.utc)
    findb["target_calendar_evidence"] = _calendar_evidence(observed_at)
    for day in findb["target_calendar_evidence"]["markets"]["TAIFEX"]["days"]:
        if day["trade_date"] == "2026-10-05":
            day.update(day_status="closed", is_open=False)
    assert module._expected_target(findb, "taifex", observed_at=observed_at) == "2026-10-02"
    for day in findb["target_calendar_evidence"]["markets"]["TW"]["days"]:
        if day["trade_date"] == "2026-10-05":
            day["session_close"] = "19:00"
    assert module._expected_target(findb, "finlab", observed_at=observed_at) == "2026-10-02"


def test_same_run_duplicate_receipt_preserves_coverage_and_rejects_inconsistency() -> None:
    module, findb, fetcher, observed_at = _pilot_package()
    run = findb["feeds"][-1]["runs"][0]
    receipt = deepcopy(run["attempt_receipts"][0])
    receipt.update(attempt_id="duplicate", status="duplicate")
    run["attempt_receipts"].append(receipt)
    assert (
        module._assessment(findb, fetcher, observed_at=observed_at)["pilot_functional_complete"]
        is True
    )
    for field, invalid in (
        ("status", "rejected"),
        ("request_sha256", "b" * 64),
        ("run_id", "other"),
        ("dataset_key", "other"),
    ):
        altered = deepcopy(findb)
        altered["feeds"][-1]["runs"][0]["attempt_receipts"][-1][field] = invalid
        assert (
            module._assessment(altered, fetcher, observed_at=observed_at)[
                "pilot_functional_complete"
            ]
            is False
        )
    inconsistent = deepcopy(run)
    inconsistent.update(run_id="failed-run", status="failed")
    findb["feeds"][-1]["runs"].append(inconsistent)
    assert (
        module._assessment(findb, fetcher, observed_at=observed_at)["pilot_functional_complete"]
        is False
    )


@pytest.mark.parametrize("status", [None, "blocked"])
def test_us_new_target_required_at_0830_before_delivery_deadline(status) -> None:
    module, findb, fetcher, _ = _pilot_package()
    observed_at = datetime(2026, 10, 6, 0, 30, tzinfo=timezone.utc)
    findb["target_calendar_evidence"] = _calendar_evidence(observed_at)
    if status:
        fetcher["twelve_data"]["recent_trade_dates"].append(
            {
                "target_date": "2026-10-05",
                "status": status,
            }
        )
    acceptance = module._functional_acceptance(findb, fetcher, observed_at=observed_at)
    assert acceptance["twelve_data/us_equity_eod"] is False
    assert module._expected_target(findb, "twelve_data", observed_at=observed_at) == "2026-10-05"


@pytest.mark.parametrize("schema", ["market_eod", "market_minute", "futures_eod"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "other_provider"),
        ("source_fetched_at", "2026-10-02T10:00:01Z"),
        ("close", "101.00000001"),
        ("volume", 11),
    ],
)
def test_serve_probe_rejects_same_key_different_provenance_or_content(
    monkeypatch, schema, field, value
):
    exporter = _load(
        "negative_serve", REPO_ROOT / "backend/scripts/export_staging_feed_evidence.py"
    )
    record = _sample_record(schema)
    sample = {
        "sample_instrument_id": record["instrument_id"],
        "sample_trade_date": record["trade_date"],
        "sample_run_id": "run",
        "sample_record": record,
        "sample_contract_code": "TX:202610",
        "sample_session": "regular",
    }
    returned = {**record, field: value}

    class Response(BytesIO):
        status = 200

    monkeypatch.setenv("FINDB_STATIC_CACHE_SERVE_API_KEY", "fixture-key")
    monkeypatch.setattr(
        exporter,
        "urlopen",
        lambda *args, **kwargs: Response(
            json.dumps({"success": True, "data": [returned]}).encode()
        ),
    )
    probe = exporter._serve_probe(schema, sample)
    assert probe["success"] is False and probe["run_id"] is None
    assert probe["returned_fingerprint"] is None


def test_minute_probe_matches_exact_bar_and_normalizes_decimals_and_aware_times(monkeypatch):
    exporter = _load("exact_minute", REPO_ROOT / "backend/scripts/export_staging_feed_evidence.py")
    record = _sample_record("market_minute")
    record["open"] = Decimal("100.10000000")
    sample = {
        "sample_instrument_id": record["instrument_id"],
        "sample_trade_date": record["trade_date"],
        "sample_run_id": "run",
        "sample_record": record,
    }
    returned = {**record, "open": 100.1, "source_fetched_at": "2026-10-02T18:00:00+08:00"}
    other_bar = {**returned, "bar_start_time": "2026-10-02T01:31:00Z"}

    class Response(BytesIO):
        status = 200

    def urlopen(request, timeout):
        assert "page_size=1000" in request.full_url
        return Response(json.dumps({"success": True, "data": [other_bar, returned]}).encode())

    monkeypatch.setenv("FINDB_STATIC_CACHE_SERVE_API_KEY", "fixture-key")
    monkeypatch.setattr(exporter, "urlopen", urlopen)
    proof = exporter._serve_probe("market_minute", sample)
    assert proof["success"] is True and proof["run_id"] == "run"
    assert proof["returned_projection"]["bar_start_time"] == "2026-10-02T01:30:00.000000+00:00"
    monkeypatch.setattr(
        exporter,
        "urlopen",
        lambda *args, **kwargs: Response(
            json.dumps({"success": True, "data": [other_bar]}).encode()
        ),
    )
    assert exporter._serve_probe("market_minute", sample)["success"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "fetched_at",
        "close",
        "fingerprint",
        "sample_run",
        "current_job",
        "current_receipt",
        "current_delivery",
        "historical_receipt",
    ],
)
def test_collector_rechecks_provenance_and_current_queue_generation(mutation):
    collector, package, fetcher, observed_at = _pilot_package()
    feed = package["feeds"][-1]
    run = feed["runs"][0]
    historical = {
        **run["outbox_receipts"][0],
        "outbox_id": "old",
        "delivery_id": "old-generation",
        "status": "published",
    }
    run["outbox_receipts"].insert(0, historical)
    assert (
        collector._functional_acceptance(package, fetcher, observed_at=observed_at)[
            "taifex/tw_futures_eod"
        ]
        is True
    )
    if mutation in {"source", "fetched_at", "close"}:
        feed["serve_probe"]["returned_projection"] = {
            **feed["serve_probe"]["returned_projection"],
            {"source": "source", "fetched_at": "source_fetched_at", "close": "close"}[
                mutation
            ]: "tampered",
        }
    elif mutation == "fingerprint":
        feed["serve_probe"]["returned_fingerprint"] = "a" * 64
    elif mutation == "sample_run":
        feed["canonical_sample"]["sample_record"]["run_id"] = "other"
    elif mutation == "current_job":
        run["job_status"] = "failed"
    elif mutation == "current_receipt":
        run["outbox_receipts"][-1]["status"] = "failed"
    elif mutation == "current_delivery":
        run["job_delivery_id"] = "unknown"
    else:
        historical["job_id"] = "unrelated-job"
    assert (
        collector._functional_acceptance(package, fetcher, observed_at=observed_at)[
            "taifex/tw_futures_eod"
        ]
        is False
    )


@pytest.fixture
def local_ssm(monkeypatch, tmp_path):
    """Execute the entire generated shell command, replacing only Docker and AWS."""
    docker = tmp_path / "docker"
    docker.write_text(
        "#!" + sys.executable + "\n"
        "import os, sys\n"
        "arguments = sys.argv[1:]\n"
        "assert arguments[:2] == ['exec', '-i']\n"
        "arguments = arguments[2:]\n"
        "environment = dict(os.environ)\n"
        "while arguments[0] == '-e':\n"
        "    key, value = arguments[1].split('=', 1)\n"
        "    environment[key] = value\n"
        "    arguments = arguments[2:]\n"
        "assert arguments[1:] == ['python', '-']\n"
        "os.execve(sys.executable, [sys.executable, '-'], environment)\n"
    )
    docker.chmod(0o700)
    collector = _load(
        "transport_acceptance", REPO_ROOT / "infra/acceptance/collect_staging_feed_evidence.py"
    )
    monkeypatch.setattr(collector, "TRANSPORT_PREFIX", str(tmp_path / "findb-staging-evidence-"))
    state = {"commands": [], "responses": {}, "mutate": lambda _config, payload: payload}

    def aws_json(arguments, *, timeout=60):
        assert 1 <= timeout <= 60
        if arguments[1] == "send-command":
            parameters = json.loads(arguments[arguments.index("--parameters") + 1])
            assert parameters["executionTimeout"] == ["130"]
            command = parameters["commands"][0]
            encoded = shlex.split(command.split("\n", 1)[1])[2]
            source = base64.b64decode(encoded).decode()
            config = ast.literal_eval(source.split("\n", 1)[0].removeprefix("config = "))
            state["commands"].append(config)
            if config["operation"] == "cleanup" and state.get("cleanup_failure"):
                raise RuntimeError("cleanup unavailable")
            result = subprocess.run(
                ["sh"],
                check=False,
                capture_output=True,
                text=True,
                input=command,
                env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
                timeout=timeout,
            )
            command_id = str(len(state["commands"]))
            payload = json.loads(result.stdout) if result.returncode == 0 else None
            if payload is not None:
                if config["operation"] == "export":
                    state["envelope"] = deepcopy(payload)
                payload = state["mutate"](config, payload)
            stdout = json.dumps(payload) if payload is not None else result.stdout
            assert len(stdout) < 24_000
            state["responses"][command_id] = {
                "Status": "Success" if result.returncode == 0 else "Failed",
                "StandardOutputContent": stdout[:24_000],
            }
            return {"Command": {"CommandId": command_id}}
        assert arguments[1] == "get-command-invocation"
        return state["responses"][arguments[arguments.index("--command-id") + 1]]

    monkeypatch.setattr(collector, "_aws_json", aws_json)
    return collector, state


def _transport_probe(collector, tmp_path, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    script = tmp_path / "probe.py"
    script.write_text("import sys\nsys.stdout.buffer.write(" + repr(raw) + ")\n")
    result = collector._ssm_probe(
        region="test", instance_id="test", script=script, container="findb-ingest"
    )
    return result, raw


def _large_transport_payload():
    # Seeded noise exercises the fallback even when gzip cannot fit stdout inline.
    rng = random.Random(17)
    return {"noise": base64.b64encode(rng.randbytes(70_000)).decode()}


def test_ssm_transport_preserves_two_date_five_feed_proof(local_ssm, tmp_path):
    collector, state = local_ssm
    _module, package, fetcher, observed_at = _pilot_package()
    for feed in package["feeds"]:
        old_run = deepcopy(feed["runs"][0])
        old_run["batch_data_date"] = "2026-10-01"
        old_run["run_id"] += ":previous"
        for record in old_run["canonical_coverage"]:
            record["trade_date"] = "2026-10-01"
        for receipt in old_run["attempt_receipts"]:
            receipt["run_id"] = old_run["run_id"]
        for receipt in old_run["outbox_receipts"]:
            receipt["run_id"] = old_run["run_id"]
        feed["runs"].append(old_run)
    assert sum(len(feed["runs"]) for feed in package["feeds"]) == 10
    assert collector._assessment(package, fetcher, observed_at=observed_at)[
        "pilot_functional_complete"
    ]
    result, raw = _transport_probe(collector, tmp_path, package)
    assert len(raw) > 24_000
    assert result["payload"] == package
    assert state["envelope"]["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert collector._assessment(result["payload"], fetcher, observed_at=observed_at)[
        "pilot_functional_complete"
    ]
    assert [item["operation"] for item in state["commands"]] == ["export", "cleanup"]


@pytest.mark.parametrize("large", [False, True])
def test_ssm_transport_roundtrip_inline_and_multiple_chunks(local_ssm, tmp_path, large):
    collector, state = local_ssm
    payload = _large_transport_payload() if large else {"small": "測試"}
    result, raw = _transport_probe(collector, tmp_path, payload)
    assert result["payload"] == payload
    assert state["envelope"]["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    count = state["envelope"]["chunks"]
    assert count > 1 if large else count == 1
    assert len(state["commands"]) == count + 2 if large else len(state["commands"]) == 2
    assert state["commands"][-1]["operation"] == "cleanup"
    assert not list(tmp_path.glob("findb-staging-evidence-*.gz"))


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "order",
        "duplicate",
        "tamper",
        "over_raw",
        "over_compressed",
        "over_chunks",
        "nonce",
        "decode",
        "bomb",
        "invalid_json",
        "checksum",
        "raw_checksum",
        "missing_inline",
    ],
)
def test_ssm_transport_rejects_corruption_and_cleans(local_ssm, tmp_path, mutation):
    collector, state = local_ssm

    def mutate(config, payload):
        if config["operation"] == "read" and config["index"] == 1:
            if mutation == "missing":
                payload["data"] = ""
            elif mutation == "order":
                payload["index"] += 1
            elif mutation == "duplicate":
                payload["index"] = 0
            elif mutation == "tamper":
                payload["data"] = "!" + payload["data"][1:]
        elif config["operation"] == "export":
            if mutation.startswith("over_"):
                field, maximum = {
                    "over_raw": ("raw_bytes", collector.TRANSPORT_MAX_RAW_BYTES),
                    "over_compressed": (
                        "compressed_bytes",
                        collector.TRANSPORT_MAX_COMPRESSED_BYTES,
                    ),
                    "over_chunks": ("chunks", collector.TRANSPORT_MAX_CHUNKS),
                }[mutation]
                payload[field] = maximum + 1
            elif mutation == "nonce":
                payload["nonce"] = "../../invalid"
            elif mutation == "checksum":
                payload["sha256"] = "0" * 64
            elif mutation == "raw_checksum":
                payload["raw_sha256"] = "0" * 64
            elif mutation == "missing_inline":
                payload.update(chunks=1, compressed_bytes=1, raw_bytes=1)
                payload.pop("data", None)
            elif mutation in {"decode", "bomb", "invalid_json"}:
                raw = b"x" * 100_000 if mutation == "bomb" else b"not json"
                compressed = b"invalid gzip" if mutation == "decode" else gzip.compress(raw)
                payload.update(
                    compressed_bytes=len(compressed),
                    raw_bytes=1 if mutation == "bomb" else len(raw),
                    chunks=1,
                    data=base64.b64encode(compressed).decode(),
                    sha256=hashlib.sha256(compressed).hexdigest(),
                    raw_sha256=hashlib.sha256(raw).hexdigest(),
                )
        return payload

    state["mutate"] = mutate
    with pytest.raises(RuntimeError, match="invalid SSM evidence transport"):
        _transport_probe(collector, tmp_path, _large_transport_payload())
    assert state["commands"][-1]["operation"] == "cleanup"
    assert not list(tmp_path.glob("findb-staging-evidence-*.gz"))
    if mutation.startswith("over_") or mutation == "nonce":
        assert len(state["commands"]) == 2


def test_ssm_transport_cleanup_failure_preserves_original_error(local_ssm, tmp_path):
    collector, state = local_ssm
    state["cleanup_failure"] = True
    state["mutate"] = lambda config, payload: (
        {**payload, "nonce": "invalid"} if config["operation"] == "export" else payload
    )
    with pytest.raises(RuntimeError, match="transport identity"):
        _transport_probe(collector, tmp_path, _large_transport_payload())
    assert state["commands"][-1]["operation"] == "cleanup"


def test_ssm_transport_rejects_invalid_path_and_non_json_export(local_ssm, tmp_path):
    collector, state = local_ssm
    with pytest.raises(ValueError, match="invalid evidence transport target"):
        collector._transport_command("findb-ingest", "../../bad", "read")
    script = tmp_path / "bad.py"
    script.write_text("print('not json')")
    with pytest.raises(RuntimeError, match="finished with status Failed"):
        collector._ssm_probe(
            region="test", instance_id="test", script=script, container="findb-ingest"
        )
    assert state["commands"][-1]["operation"] == "cleanup"


@pytest.mark.parametrize(
    "budget", ["TRANSPORT_MAX_RAW_BYTES", "TRANSPORT_MAX_COMPRESSED_BYTES", "TRANSPORT_MAX_CHUNKS"]
)
def test_ssm_wrapper_enforces_output_budgets(local_ssm, monkeypatch, tmp_path, budget):
    collector, state = local_ssm
    monkeypatch.setattr(collector, budget, 1000 if budget != "TRANSPORT_MAX_CHUNKS" else 1)
    with pytest.raises(RuntimeError, match="finished with status Failed"):
        _transport_probe(collector, tmp_path, _large_transport_payload())
    assert state["commands"][-1]["operation"] == "cleanup"
    assert not list(tmp_path.glob("findb-staging-evidence-*.gz"))


def test_ssm_transport_total_deadline_and_cleanup(local_ssm, monkeypatch, tmp_path):
    collector, state = local_ssm
    ticks = iter([0, 1, 1, 1, 181, 181, 181, 181, 181, 181])
    monkeypatch.setattr(collector.time, "monotonic", lambda: next(ticks, 181))
    with pytest.raises(RuntimeError, match="collection timeout"):
        _transport_probe(collector, tmp_path, _large_transport_payload())
    assert state["commands"][-1]["operation"] == "cleanup"
    assert not list(tmp_path.glob("findb-staging-evidence-*.gz"))


def test_ssm_wrapper_prunes_only_expired_owned_regular_artifacts(local_ssm, tmp_path):
    collector, _state = local_ssm
    import time

    paths = [
        Path("/tmp/findb-staging-evidence-" + f"{number:032x}" + ".gz")
        for number in (701, 702, 703)
    ]
    unrelated = tmp_path / "unrelated.gz"
    unrelated.write_bytes(b"untouched")
    try:
        paths[0].write_bytes(b"expired")
        paths[1].write_bytes(b"active")
        paths[2].symlink_to(unrelated)
        past = time.time() - 86401
        os.utime(paths[0], (past, past))
        os.utime(paths[2], (past, past), follow_symlinks=False)
        _transport_probe(collector, tmp_path, {"ok": True})
        assert not paths[0].exists()
        assert paths[1].read_bytes() == b"active"
        assert paths[2].is_symlink()
        assert unrelated.read_bytes() == b"untouched"
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_probe_command_preserves_environment_as_literal_data(local_ssm, tmp_path):
    collector, state = local_ssm
    script = tmp_path / "environment.py"
    script.write_text(
        "import json, os\nprint(json.dumps({'provider': os.environ['FINDB_EVIDENCE_PROVIDER'], 'value': os.environ['LITERAL']}))"
    )
    value = "quote ' $(do-not-execute); newline\n資料"
    nonce = "a" * 32
    _command_id, envelope = collector._ssm_json(
        region="test",
        instance_id="test",
        deadline=collector.time.monotonic() + 60,
        command=collector._probe_command(
            script, "findb-fetcher-shioaji-scheduler", "shioaji", {"LITERAL": value}, nonce=nonce
        ),
    )
    assert collector._decode_transport(envelope, [envelope["data"]], nonce) == {
        "provider": "shioaji",
        "value": value,
    }
    assert state["commands"][0]["nonce"] == nonce
