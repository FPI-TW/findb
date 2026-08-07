from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from findb_fetcher.contracts import (
    ContractChecksumError,
    ContractRegistry,
    ContractValidationError,
)

LEGACY_SLOT_ALIASES = (
    ("us_0600", "western_markets_window"),
    ("global_0815", "global_markets_window"),
    ("tw_1430", "taiwan_market_window"),
    ("asia_1630", "asia_pacific_markets_window"),
)


def test_valid_request_satisfies_local_contract(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    registry = ContractRegistry(contracts_dir)

    registry.validate("market_eod", 1, market_request)


def test_invalid_request_is_rejected_by_local_contract(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    registry = ContractRegistry(contracts_dir)
    market_request["payload"]["data"][0]["unexpected"] = True

    with pytest.raises(ContractValidationError, match="unexpected"):
        registry.validate("market_eod", 1, market_request)


@pytest.mark.parametrize(("legacy_slot", "canonical_slot"), LEGACY_SLOT_ALIASES)
def test_legacy_delivery_slot_validates_without_mutating_prepared_request(
    contracts_dir: Path,
    market_request: dict[str, Any],
    legacy_slot: str,
    canonical_slot: str,
) -> None:
    registry = ContractRegistry(contracts_dir)
    market_request["delivery"] = {
        "slot_id": legacy_slot,
        "scheduled_for": "2026-07-23T14:30:00Z",
        "target_data_date": "2026-07-23",
        "work_item_id": "2330",
    }
    before = deepcopy(market_request)
    before_bytes = json.dumps(market_request, sort_keys=True, separators=(",", ":")).encode()

    registry.validate("market_eod", 1, market_request)

    assert market_request == before
    assert (
        json.dumps(market_request, sort_keys=True, separators=(",", ":")).encode() == before_bytes
    )
    assert market_request["delivery"]["slot_id"] == legacy_slot
    assert canonical_slot != legacy_slot


def test_canonical_delivery_slot_validates_unchanged(
    contracts_dir: Path,
    market_request: dict[str, Any],
) -> None:
    registry = ContractRegistry(contracts_dir)
    market_request["delivery"] = {
        "slot_id": "taiwan_market_window",
        "scheduled_for": "2026-07-23T14:30:00Z",
        "target_data_date": "2026-07-23",
        "work_item_id": "2330",
    }
    before = deepcopy(market_request)

    registry.validate("market_eod", 1, market_request)

    assert market_request == before


def test_unknown_delivery_slot_remains_invalid(
    contracts_dir: Path,
    market_request: dict[str, Any],
) -> None:
    registry = ContractRegistry(contracts_dir)
    market_request["delivery"] = {
        "slot_id": "western_markets_window_v99",
        "scheduled_for": "2026-07-23T14:30:00Z",
        "target_data_date": "2026-07-23",
        "work_item_id": "2330",
    }

    with pytest.raises(ContractValidationError, match="slot_id"):
        registry.validate("market_eod", 1, market_request)


def test_modified_contract_fails_manifest_checksum(tmp_path: Path, contracts_dir: Path) -> None:
    copied_contracts = tmp_path / "contracts"
    shutil.copytree(contracts_dir, copied_contracts)
    schema_path = copied_contracts / "market_eod" / "v1.schema.json"
    schema = json.loads(schema_path.read_text())
    schema["description"] = "tampered"
    schema_path.write_text(json.dumps(schema))

    with pytest.raises(ContractChecksumError, match="checksum mismatch"):
        ContractRegistry(copied_contracts)
