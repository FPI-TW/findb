from __future__ import annotations

import json
from pathlib import Path

import pytest

from findb_fetcher.finlab_universe import FinLabUniverseError, load_finlab_universe

CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "finlab_tw_review_required.v1.json"


def _config() -> dict[str, object]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, value: dict[str, object]) -> Path:
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_loader_accepts_exact_reviewed_pilot_and_exposes_one_state_work_item() -> None:
    universe = load_finlab_universe(CONFIG_PATH)

    assert universe.provider == "finlab"
    assert universe.dataset == "tw_equity_eod"
    assert universe.market == "TW"
    assert universe.asset_class == "equity"
    assert universe.currency == "TWD"
    assert {(item.source_symbol, item.canonical_symbol) for item in universe.symbols} == {
        ("2330", "2330"),
        ("2317", "2317"),
    }
    state_universe = universe.as_scheduler_universe()
    assert len(state_universe.symbols) == 1
    assert state_universe.symbols[0].symbol == "tw_equity_eod"
    assert state_universe.estimated_credits == 5


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("unexpected", True),
        lambda value: value.__setitem__("dataset", "tw_etf_eod"),
        lambda value: value.__setitem__("currency", "USD"),
        lambda value: value["symbols"].append(  # type: ignore[union-attr]
            {"source_symbol": "0050", "canonical_symbol": "0050"}
        ),
        lambda value: value["symbols"].__setitem__(  # type: ignore[index]
            1, {"source_symbol": "2330", "canonical_symbol": "2330"}
        ),
        lambda value: value["symbols"][0].__setitem__("extra", "nope"),  # type: ignore[index]
        lambda value: value["field_datasets"].__setitem__(  # type: ignore[union-attr]
            "close", "price:開盤價"
        ),
    ],
)
def test_loader_rejects_unknown_duplicate_extra_or_out_of_scope_membership(
    tmp_path: Path, mutate
) -> None:
    value = _config()
    mutate(value)
    with pytest.raises(FinLabUniverseError):
        load_finlab_universe(_write(tmp_path, value))


def test_loader_rejects_missing_required_fields_and_duplicate_json_keys(tmp_path: Path) -> None:
    value = _config()
    del value["currency"]
    with pytest.raises(FinLabUniverseError, match="keys"):
        load_finlab_universe(_write(tmp_path, value))

    duplicate = '{"manifest_version":1,"manifest_version":1}'
    path = tmp_path / "duplicate.json"
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(FinLabUniverseError, match="unique"):
        load_finlab_universe(path)
