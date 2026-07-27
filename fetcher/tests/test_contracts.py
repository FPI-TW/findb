from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from findb_fetcher.contracts import (
    ContractChecksumError,
    ContractRegistry,
    ContractValidationError,
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


def test_modified_contract_fails_manifest_checksum(tmp_path: Path, contracts_dir: Path) -> None:
    copied_contracts = tmp_path / "contracts"
    shutil.copytree(contracts_dir, copied_contracts)
    schema_path = copied_contracts / "market_eod" / "v1.schema.json"
    schema = json.loads(schema_path.read_text())
    schema["description"] = "tampered"
    schema_path.write_text(json.dumps(schema))

    with pytest.raises(ContractChecksumError, match="checksum mismatch"):
        ContractRegistry(copied_contracts)
