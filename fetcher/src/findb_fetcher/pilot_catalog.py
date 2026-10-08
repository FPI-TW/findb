"""Fail-closed staging pilot catalog shared by readiness and acceptance tooling."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

from findb_fetcher.config import app_environment

SUPPORTED_PILOTS = {
    "twelve_data": (("us_equity_eod", "market_eod"),),
    "finlab": (("tw_equity_eod", "market_eod"),),
    "shioaji": (("tw_equity_minute", "market_minute"), ("tw_etf_minute", "market_minute")),
    "taifex": (("tw_futures_eod", "futures_eod"),),
}
PILOT_RUNTIMES = {
    "twelve_data": ("findb-fetch-scheduler", "findb_fetcher.scheduler_cli"),
    "finlab": ("findb-fetch-finlab-scheduler", "findb_fetcher.finlab_scheduler_cli"),
    "shioaji": ("findb-fetch-shioaji-scheduler", "findb_fetcher.shioaji_scheduler_cli"),
    "taifex": ("findb-fetch-taifex-pilot", "findb_fetcher.taifex_pilot"),
}


def validate_staging_pilots(directory: Path) -> dict[str, Any]:
    """Every provider needs a bounded pilot, supported schema and startup acceptance."""
    if app_environment("staging") != "staging":
        raise ValueError("staging pilots require the exact staging deployment target")
    raw = (directory / "staging_provider_pilots.v1.json").read_bytes()
    if len(raw) > 16384:
        raise ValueError("pilot catalog exceeds byte bound")
    catalog = json.loads(raw)
    if catalog.get("version") != 1 or set(catalog.get("providers", {})) != set(SUPPORTED_PILOTS):
        raise ValueError("every supported provider must have a staging pilot")
    for provider, feeds in SUPPORTED_PILOTS.items():
        spec = catalog["providers"][provider]
        command, module = PILOT_RUNTIMES[provider]
        instruments = spec.get("instruments")
        if (
            not isinstance(instruments, list)
            or not 1 <= len(instruments) <= 2
            or len(set(instruments)) != len(instruments)
            or tuple((feed["dataset_key"], feed["schema_id"]) for feed in spec["feeds"]) != feeds
            or spec.get("schema_version") != 1
            or spec.get("runtime_command") != command
            or importlib.util.find_spec(module) is None
            or spec.get("fixture_acceptance") != f"test_staging_pilots:{provider}"
            or spec.get("live_acceptance") != "source_raw_terminal_canonical_serve"
        ):
            raise ValueError("provider pilot scope or acceptance metadata invalid")
    required_files = {
        "daily_scheduler.staging.v3.json",
        "twelve_data_us_staging_pilot.v2.json",
        "finlab_tw_review_required.v1.json",
        "shioaji_tw_staging_pilot.v3.json",
        "taifex_tw_staging_pilot.v1.json",
    }
    if set(catalog.get("files", {})) != required_files:
        raise ValueError("pilot catalog configuration bindings incomplete")
    for name, digest in catalog["files"].items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("pilot catalog path invalid")
        if hashlib.sha256((directory / path).read_bytes()).hexdigest() != digest:
            raise ValueError("pilot configuration differs from catalog binding")
    # Scope counts are actual config counts, including all minute sequences.
    twelve = json.loads((directory / "twelve_data_us_staging_pilot.v2.json").read_bytes())
    finlab = json.loads((directory / "finlab_tw_review_required.v1.json").read_bytes())
    minute = json.loads((directory / "shioaji_tw_staging_pilot.v3.json").read_bytes())
    futures = json.loads((directory / "taifex_tw_staging_pilot.v1.json").read_bytes())
    actual = {
        "twelve_data": [row["symbol"] for row in twelve["symbols"]],
        "finlab": [row["source_symbol"] for row in finlab["symbols"]],
        "shioaji": [row["symbol"] for row in minute["sequences"]],
        "taifex": futures["products"],
    }
    if any(
        actual[provider] != spec["instruments"] for provider, spec in catalog["providers"].items()
    ):
        raise ValueError("pilot instruments differ from validated catalog")
    if any(row["sequence"] != 1 or row["sequence_count"] != 1 for row in minute["sequences"]):
        raise ValueError("staging minute snapshots must each contain one instrument")
    if twelve["limits"] != {
        "max_symbols_per_run": 2,
        "max_records_per_symbol": 1,
        "max_total_records_per_run": 2,
        "max_date_span_days": 1,
        "max_credits_per_run": 2,
    }:
        raise ValueError("staging daily instrument/date/record bounds changed")
    if any(
        futures.get(key) != limit
        for key, limit in {
            "max_requests_per_date": 6,
            "max_response_bytes": 8388608,
            "max_records_per_date": 4,
            "max_attempts_per_date": 3,
            "max_date_span_days": 1,
        }.items()
    ):
        raise ValueError("TAIFEX staging bounds changed")
    return catalog
