"""Registry and validation entry point for versioned ingress contracts."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.schemas.ingress import (
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


class DatasetContractDefaults(BaseModel):
    """Canonical scope required by schema-based normalizers."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    market: str
    asset_class: str
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("market")
    @classmethod
    def normalize_declared_market(cls, value: str) -> str:
        return normalize_market(value)

    @field_validator("asset_class")
    @classmethod
    def normalize_declared_asset_class(cls, value: str) -> str:
        return normalize_asset_class(value)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None


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
