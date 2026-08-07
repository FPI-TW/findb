"""Strict loading and validation of immutable ingress contract artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for

from findb_fetcher.schedule import LEGACY_SLOT_ID_MAP

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ROOT_KEYS = {"manifest_version", "contracts"}
_ENTRY_KEYS = {"path", "schema_id", "schema_version", "sha256"}


class ContractError(ValueError):
    """Base class for local contract failures."""


class ContractManifestError(ContractError):
    """The manifest or one of its schema declarations is malformed."""


class ContractChecksumError(ContractError):
    """A contract file no longer matches its immutable manifest digest."""


class ContractNotFoundError(ContractError):
    """The requested schema/version pair is not present in the manifest."""


class ContractValidationError(ContractError):
    """A delivery does not satisfy its declared JSON Schema."""


class _DuplicateJSONKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Contract:
    schema_id: str
    schema_version: int
    path: Path
    sha256: str
    schema: dict[str, Any]


class ContractRegistry:
    """Verified immutable contract artifacts indexed by schema identity."""

    def __init__(self, contracts_dir: Path) -> None:
        self._contracts_dir = contracts_dir.resolve()
        manifest = self._read_json(self._contracts_dir / "manifest.json", "manifest")
        self._contracts = self._load_manifest(manifest)

    def get(self, schema_id: str, schema_version: int) -> Contract:
        try:
            return self._contracts[(schema_id, schema_version)]
        except KeyError as exc:
            raise ContractNotFoundError(
                f"contract {schema_id}.v{schema_version} is not in the manifest"
            ) from exc

    def validate(self, schema_id: str, schema_version: int, instance: dict[str, Any]) -> None:
        contract = self.get(schema_id, schema_version)
        validator_class = validator_for(contract.schema)
        validator = validator_class(contract.schema, format_checker=FormatChecker())
        validation_instance = _legacy_delivery_compatibility_copy(instance)
        try:
            validator.validate(validation_instance)
        except ValidationError as exc:
            location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
            raise ContractValidationError(f"{location}: {exc.message}") from exc

    def _load_manifest(self, manifest: dict[str, Any]) -> dict[tuple[str, int], Contract]:
        if set(manifest) != _ROOT_KEYS:
            raise ContractManifestError(f"manifest keys must be exactly {sorted(_ROOT_KEYS)}")
        if type(manifest["manifest_version"]) is not int or manifest["manifest_version"] != 1:
            raise ContractManifestError("manifest_version must be integer 1")
        entries = manifest["contracts"]
        if not isinstance(entries, list) or not entries:
            raise ContractManifestError("contracts must be a non-empty list")

        contracts: dict[tuple[str, int], Contract] = {}
        seen_paths: set[str] = set()
        for index, entry in enumerate(entries):
            contract = self._load_entry(index, entry)
            identity = (contract.schema_id, contract.schema_version)
            if identity in contracts:
                raise ContractManifestError(
                    f"duplicate contract identity {contract.schema_id}.v{contract.schema_version}"
                )
            relative_path = contract.path.relative_to(self._contracts_dir).as_posix()
            if relative_path in seen_paths:
                raise ContractManifestError(f"duplicate contract path {relative_path}")
            contracts[identity] = contract
            seen_paths.add(relative_path)
        return contracts

    def _load_entry(self, index: int, entry: Any) -> Contract:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            raise ContractManifestError(
                f"contracts[{index}] keys must be exactly {sorted(_ENTRY_KEYS)}"
            )

        relative_path = entry["path"]
        schema_id = entry["schema_id"]
        schema_version = entry["schema_version"]
        expected_sha256 = entry["sha256"]
        if not isinstance(relative_path, str) or not _is_safe_relative_path(relative_path):
            raise ContractManifestError(f"contracts[{index}].path is not a safe relative path")
        if not isinstance(schema_id, str) or not re.fullmatch(r"[a-z0-9_]+", schema_id):
            raise ContractManifestError(f"contracts[{index}].schema_id is invalid")
        if type(schema_version) is not int or schema_version < 1:
            raise ContractManifestError(
                f"contracts[{index}].schema_version must be a positive integer"
            )
        if not isinstance(expected_sha256, str) or not _SHA256_PATTERN.fullmatch(expected_sha256):
            raise ContractManifestError(f"contracts[{index}].sha256 is invalid")

        contract_path = (self._contracts_dir / relative_path).resolve()
        try:
            contract_path.relative_to(self._contracts_dir)
        except ValueError as exc:
            raise ContractManifestError(
                f"contracts[{index}].path resolves outside contracts directory"
            ) from exc
        if not contract_path.is_file():
            raise ContractManifestError(f"contract file does not exist: {relative_path}")

        raw_schema = contract_path.read_bytes()
        actual_sha256 = hashlib.sha256(raw_schema).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ContractChecksumError(
                f"checksum mismatch for {relative_path}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
        schema = self._parse_json(raw_schema, f"contract {relative_path}")
        validator_class = validator_for(schema)
        try:
            validator_class.check_schema(schema)
        except SchemaError as exc:
            raise ContractManifestError(
                f"contract {relative_path} is not a valid JSON Schema: {exc.message}"
            ) from exc
        metadata = schema.get("x-findb-contract")
        expected_metadata = {
            "schema_id": schema_id,
            "schema_version": schema_version,
        }
        if metadata != expected_metadata:
            raise ContractManifestError(
                f"contract {relative_path} identity does not match its manifest entry"
            )
        return Contract(
            schema_id=schema_id,
            schema_version=schema_version,
            path=contract_path,
            sha256=expected_sha256,
            schema=schema,
        )

    @classmethod
    def _read_json(cls, path: Path, label: str) -> dict[str, Any]:
        if not path.is_file():
            raise ContractManifestError(f"{label} file does not exist: {path}")
        return cls._parse_json(path.read_bytes(), label)

    @staticmethod
    def _parse_json(raw: bytes, label: str) -> dict[str, Any]:
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKeyError) as exc:
            raise ContractManifestError(f"{label} is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ContractManifestError(f"{label} must contain a JSON object")
        return value


def _is_safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and "\\" not in value
        and ".." not in path.parts
        and path.parts[-1].endswith(".schema.json")
    )


def _legacy_delivery_compatibility_copy(instance: dict[str, Any]) -> dict[str, Any]:
    """Return a validation-only copy for persisted pre-v2 slot metadata.

    Prepared requests are immutable replay artifacts, so the legacy alias is
    normalized only in the object handed to JSON Schema validation.  New
    requests and arbitrary slot IDs continue through unchanged and are judged
    strictly by the canonical schema.
    """
    delivery = instance.get("delivery")
    if not isinstance(delivery, dict):
        return instance
    legacy_slot_id = delivery.get("slot_id")
    if not isinstance(legacy_slot_id, str):
        return instance
    canonical_slot_id = LEGACY_SLOT_ID_MAP.get(legacy_slot_id)
    if canonical_slot_id is None:
        return instance
    validation_instance = deepcopy(instance)
    validation_instance["delivery"]["slot_id"] = canonical_slot_id
    return validation_instance


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result
