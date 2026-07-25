from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from findb_fetcher.universe import UniverseError, load_symbol_universe

CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "configs" / "twelve_data_us_common_stocks.v1.json"
)


@pytest.fixture
def universe_value() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, value: object) -> Path:
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_repository_universe_is_strict_and_credit_bounded() -> None:
    universe = load_symbol_universe(CONFIG_PATH)

    assert universe.universe_version == 1
    assert universe.universe_id == "twelve_data_us_common_stocks_v1"
    assert universe.dataset_key == "us_equity_eod"
    assert [member.symbol for member in universe.symbols] == ["AAPL", "MSFT", "NVDA"]
    assert universe.estimated_credits == 3
    assert universe.estimated_credits <= universe.limits.max_credits_per_run
    assert len(universe.symbols) <= universe.limits.max_symbols_per_run


def test_repository_universe_contains_no_credential_fields() -> None:
    raw = CONFIG_PATH.read_text(encoding="utf-8").lower()

    assert "api_key" not in raw
    assert "secret" not in raw
    assert "token" not in raw


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unexpected_secret": "no"}),
        lambda value: value.update({"universe_version": 2}),
        lambda value: value.update({"provider": "other"}),
        lambda value: value.update({"dataset_key": "other"}),
        lambda value: value.update({"market": "TW"}),
        lambda value: value.update({"credit_cost_per_symbol": 2}),
        lambda value: value["limits"].update({"max_symbols_per_run": 6}),
        lambda value: value["limits"].update({"max_records_per_symbol": 5001}),
        lambda value: value["limits"].update({"max_credits_per_run": 6}),
        lambda value: value["symbols"].append(deepcopy(value["symbols"][0])),
        lambda value: value["symbols"][0].update({"symbol": "aapl"}),
        lambda value: value["symbols"][0].update({"exchange": "NASDAQ\nbad"}),
    ],
)
def test_invalid_or_unbounded_universe_is_rejected(
    tmp_path: Path,
    universe_value: dict[str, Any],
    mutation: Any,
) -> None:
    mutation(universe_value)

    with pytest.raises(UniverseError):
        load_symbol_universe(_write_config(tmp_path, universe_value))


def test_symbol_and_credit_run_limits_are_enforced(
    tmp_path: Path,
    universe_value: dict[str, Any],
) -> None:
    universe_value["limits"]["max_symbols_per_run"] = 2
    with pytest.raises(UniverseError, match="symbol count"):
        load_symbol_universe(_write_config(tmp_path, universe_value))

    universe_value["limits"]["max_symbols_per_run"] = 3
    universe_value["limits"]["max_credits_per_run"] = 2
    with pytest.raises(UniverseError, match="estimated credits"):
        load_symbol_universe(_write_config(tmp_path, universe_value))


def test_duplicate_json_key_and_oversized_file_are_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"universe_version":1,"universe_version":1}', encoding="utf-8")
    with pytest.raises(UniverseError, match="unique keys"):
        load_symbol_universe(duplicate)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (64 * 1024 + 1))
    with pytest.raises(UniverseError, match="size limit"):
        load_symbol_universe(oversized)


def test_universe_query_bounds_require_an_explicit_bounded_shape() -> None:
    universe = load_symbol_universe(CONFIG_PATH)

    universe.validate_query_bounds(
        start_date=date(2024, 1, 1),
        end_date=date(2025, 1, 1),
        outputsize=None,
    )
    universe.validate_query_bounds(
        start_date=None,
        end_date=None,
        outputsize=260,
    )

    invalid = [
        (None, None, None),
        (date(2024, 1, 1), None, None),
        (None, date(2024, 1, 2), None),
        (date(2024, 1, 2), date(2024, 1, 1), None),
        (date(2024, 1, 1), date(2025, 1, 2), None),
        (date(2024, 1, 1), date(2024, 1, 2), 1),
        (None, None, 261),
    ]
    for start_date, end_date, outputsize in invalid:
        with pytest.raises(UniverseError):
            universe.validate_query_bounds(
                start_date=start_date,
                end_date=end_date,
                outputsize=outputsize,
            )
