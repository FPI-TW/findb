"""Registry and validation entry point for versioned ingress contracts."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.schemas.ingress import (
    CurrencyCode,
    FuturesContinuousEODIngressRequest,
    IngressRequestV1,
    MarketEODIngressRequest,
)
from app.vocabulary import normalize_asset_class, normalize_market

ContractKey = tuple[str, int]
ContractModel = type[MarketEODIngressRequest] | type[FuturesContinuousEODIngressRequest]
PositiveStrictInt = Annotated[StrictInt, Field(ge=1)]

_CONTRACT_MODELS: Mapping[ContractKey, ContractModel] = MappingProxyType(
    {
        ("market_eod", 1): MarketEODIngressRequest,
        ("futures_continuous_eod", 1): FuturesContinuousEODIngressRequest,
    }
)


class UnsupportedIngressContractError(ValueError):
    """Raised when the requested schema id/version is not registered."""


class CurrencyRequiredError(ValueError):
    """Raised when neither dataset context nor market rows provide currency."""


class DatasetContractDefaults(BaseModel):
    """Canonical scope required by schema-based normalizers."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    market: str
    asset_class: str
    currency: CurrencyCode | None = None

    @field_validator("market")
    @classmethod
    def normalize_declared_market(cls, value: str) -> str:
        return normalize_market(value)

    @field_validator("asset_class")
    @classmethod
    def normalize_declared_asset_class(cls, value: str) -> str:
        return normalize_asset_class(value)


class DatasetContractDeclaration(BaseModel):
    """Typed projection of contract-related ``dataset_registry.config`` fields."""

    model_config = ConfigDict(extra="ignore")

    schema_id: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$")
    accepted_schema_versions: list[PositiveStrictInt] = Field(min_length=1)
    current_schema_version: PositiveStrictInt
    schema_enforcement: Literal["audit", "enforce"] = "audit"
    defaults: DatasetContractDefaults

    @model_validator(mode="after")
    def validate_versions(self) -> "DatasetContractDeclaration":
        if any(version < 1 for version in self.accepted_schema_versions):
            raise ValueError("accepted_schema_versions must contain positive integers")
        if len(self.accepted_schema_versions) != len(set(self.accepted_schema_versions)):
            raise ValueError("accepted_schema_versions must not contain duplicates")
        if self.current_schema_version not in self.accepted_schema_versions:
            raise ValueError("current_schema_version must be accepted")
        return self


def supported_contracts() -> tuple[ContractKey, ...]:
    """Return stable, sorted identifiers for every registered contract."""
    return tuple(sorted(_CONTRACT_MODELS))


def get_contract_model(schema_id: str, schema_version: int) -> ContractModel:
    """Resolve a typed request model from an explicit contract identifier."""
    try:
        return _CONTRACT_MODELS[(schema_id, schema_version)]
    except KeyError as exc:
        raise UnsupportedIngressContractError(
            f"Unsupported ingress contract: {schema_id}.v{schema_version}"
        ) from exc


def get_contract_json_schema(schema_id: str, schema_version: int) -> dict[str, Any]:
    """Return the deterministic, version-addressed JSON Schema for a contract."""
    model = get_contract_model(schema_id, schema_version)
    schema = model.model_json_schema(mode="validation")
    semantic_rules: list[dict[str, Any]] = [
        {
            "id": "envelope.fetched_at.timezone_aware",
            "scope": "fetched_at",
            "description": "fetched_at must include a timezone and is normalized to UTC",
            "parameters": {
                "format": "iso8601_date_time",
                "timezone_offset": "required",
                "normalization": "utc",
            },
            "context_dependencies": [],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "batch.sequence.co_presence",
            "scope": "payload.batch",
            "description": "sequence and sequence_count must be provided together",
            "parameters": {
                "fields": ["sequence", "sequence_count"],
                "operator": "co_present",
            },
            "context_dependencies": [],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "batch.sequence.order",
            "scope": "payload.batch",
            "description": "sequence must not exceed sequence_count",
            "parameters": {
                "left": "sequence",
                "operator": "less_than_or_equal",
                "right": "sequence_count",
            },
            "context_dependencies": [],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "batch.coverage.co_presence",
            "scope": "payload.batch",
            "description": "coverage_start_date and coverage_end_date must be provided together",
            "parameters": {
                "fields": ["coverage_start_date", "coverage_end_date"],
                "operator": "co_present",
            },
            "context_dependencies": [],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "batch.coverage.order",
            "scope": "payload.batch",
            "description": "coverage_start_date must not follow coverage_end_date",
            "parameters": {
                "left": "coverage_start_date",
                "operator": "less_than_or_equal",
                "right": "coverage_end_date",
            },
            "context_dependencies": [],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "batch.backfill.data_date_equals_coverage_end",
            "scope": "payload.batch",
            "description": "backfill data_date must equal coverage_end_date when coverage is set",
            "parameters": {
                "when": {"delivery_mode": "backfill", "coverage_end_date": "present"},
                "left": "data_date",
                "operator": "equal",
                "right": "coverage_end_date",
            },
            "context_dependencies": [],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "row.ohlc.high_bound",
            "scope": "payload.data[*]",
            "description": "high must not be below any provided open, low, or close",
            "parameters": {
                "left": "high",
                "operator": "greater_than_or_equal_each_present",
                "right": ["open", "low", "close"],
            },
            "context_dependencies": [],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "row.ohlc.low_bound",
            "scope": "payload.data[*]",
            "description": "low must not be above any provided open, high, or close",
            "parameters": {
                "left": "low",
                "operator": "less_than_or_equal_each_present",
                "right": ["open", "high", "close"],
            },
            "context_dependencies": [],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "payload.coverage.contains_row_dates",
            "scope": "payload",
            "description": "coverage is required for other dates and must contain every row date",
            "parameters": {
                "operator": "range_contains_all",
                "data_date": "batch.data_date",
                "coverage_start": "batch.coverage_start_date",
                "coverage_end": "batch.coverage_end_date",
                "row_date": "data[*].trade_date",
            },
            "context_dependencies": [],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "payload.declared_record_count.matches_data",
            "scope": "payload",
            "description": "declared_record_count must equal the data array length",
            "parameters": {
                "declared": "batch.declared_record_count",
                "operator": "equal",
                "actual": "length(data)",
            },
            "context_dependencies": [],
            "error_code": "DECLARED_RECORD_COUNT_MISMATCH",
        },
        {
            "id": "payload.delivery_key.unique",
            "scope": "payload.data",
            "description": "delivery natural keys must be unique within the data array",
            "parameters": {
                "operator": "unique_by",
                "fields": ["symbol", "trade_date"],
            },
            "context_dependencies": [],
            "error_code": "DUPLICATE_DELIVERY_KEY",
        },
        {
            "id": "payload.data.max_items",
            "scope": "payload.data",
            "description": "data array length is bounded by the active server setting",
            "parameters": {"operator": "max_items", "minimum_effective_limit": 1},
            "context_dependencies": [{"kind": "runtime_setting", "name": "SOURCE_MAX_DATA_ITEMS"}],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "payload.serialized.max_bytes",
            "scope": "payload",
            "description": "compact UTF-8 JSON encoding is bounded by the active server setting",
            "parameters": {
                "operator": "max_bytes",
                "encoding": "utf-8",
                "serialization": "compact_json",
                "ensure_ascii": False,
                "non_native_values": "stringified",
                "minimum_effective_limit": 1,
                "enforcement_stage": "after_attempt_contract_validation",
            },
            "context_dependencies": [
                {"kind": "runtime_setting", "name": "SOURCE_MAX_PAYLOAD_BYTES"}
            ],
            "error_code": "INGRESS_SCHEMA_INVALID",
        },
        {
            "id": "request.body.max_bytes",
            "scope": "request",
            "description": "known Content-Length is bounded before endpoint processing",
            "parameters": {
                "operator": "max_bytes",
                "measurement": "content_length_header",
                "minimum_effective_limit": 1,
                "enforcement_stage": "before_attempt_middleware",
            },
            "context_dependencies": [
                {"kind": "runtime_setting", "name": "SOURCE_MAX_PAYLOAD_BYTES"}
            ],
            "error_code": None,
            "response": {
                "http_status": 413,
                "envelope": "middleware_detail",
                "durable_attempt": False,
            },
        },
    ]
    if schema_id == "market_eod":
        semantic_rules.append(
            {
                "id": "market.currency.row_or_dataset_default",
                "scope": "payload.data[*]",
                "description": "every row needs currency when the dataset has no default",
                "parameters": {
                    "row_field": "currency",
                    "dataset_field": "defaults.currency",
                    "operator": "row_present_or_dataset_default",
                },
                "context_dependencies": [{"kind": "dataset_context", "path": "defaults.currency"}],
                "error_code": "CURRENCY_REQUIRED",
            }
        )
    if schema_id == "futures_continuous_eod":
        semantic_rules.append(
            {
                "id": "futures.currency.dataset_default_required",
                "scope": "dataset",
                "description": "continuous futures require a dataset default currency",
                "parameters": {
                    "field": "defaults.currency",
                    "operator": "required",
                },
                "context_dependencies": [{"kind": "dataset_context", "path": "defaults.currency"}],
                "error_code": "CURRENCY_REQUIRED",
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:findb:ingress-contract:{schema_id}:v{schema_version}",
        "x-findb-contract": {
            "schema_id": schema_id,
            "schema_version": schema_version,
        },
        "x-findb-semantic-rules": semantic_rules,
        **schema,
    }


def validate_ingress_request(value: Mapping[str, Any]) -> IngressRequestV1:
    """Dispatch and validate a raw request without guessing its schema version."""
    schema_id = value.get("schema_id")
    schema_version = value.get("schema_version")
    if (
        not isinstance(schema_id, str)
        or not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
    ):
        raise UnsupportedIngressContractError("schema_id and integer schema_version are required")
    model = get_contract_model(schema_id, schema_version)
    return model.model_validate(value)


def parse_dataset_contract_declaration(
    config: Mapping[str, Any] | None,
) -> Optional[DatasetContractDeclaration]:
    """Parse an optional dataset declaration; legacy datasets return ``None``."""
    if not config or "schema_id" not in config:
        return None
    declaration = DatasetContractDeclaration.model_validate(config)
    for version in declaration.accepted_schema_versions:
        get_contract_model(declaration.schema_id, version)
    return declaration


def validate_dataset_contract_scope(
    declaration: DatasetContractDeclaration,
    *,
    market: str,
    asset_class: str,
) -> None:
    """Reject registry/config scope conflicts before any canonical write is queued."""
    registry_market = normalize_market(market)
    registry_asset_class = normalize_asset_class(asset_class)
    if declaration.defaults.market != registry_market:
        raise ValueError("Dataset contract defaults.market does not match dataset_registry.market")
    if declaration.defaults.asset_class != registry_asset_class:
        raise ValueError(
            "Dataset contract defaults.asset_class does not match " "dataset_registry.asset_class"
        )


def validate_request_currency(
    declaration: DatasetContractDeclaration,
    request: IngressRequestV1,
) -> None:
    """Apply dataset-dependent currency requirements before persistence."""
    if declaration.defaults.currency is not None:
        return
    if isinstance(request, MarketEODIngressRequest) and any(
        row.currency is None for row in request.payload.data
    ):
        raise CurrencyRequiredError(
            "currency is required for every market_eod row when the dataset has no "
            "default currency"
        )
    if isinstance(request, FuturesContinuousEODIngressRequest):
        raise CurrencyRequiredError(
            "dataset default currency is required for futures_continuous_eod"
        )
