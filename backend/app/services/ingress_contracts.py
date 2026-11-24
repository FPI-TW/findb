"""Registry and validation entry point for versioned ingress contracts."""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from app.schemas.ingress import (
    INDEX_REFERENCE_SCHEMAS,
    INTRADAY_ROW_LAYOUTS,
    MINUTES_PER_DAY,
    CurrencyCode,
    ExchangeDailyQuoteIngressRequest,
    FuturesEODIngressRequest,
    IndexChangesIngressRequest,
    IndexConstituentsIngressRequest,
    IngressRequest,
    IntradayPartitionIngressRequest,
    MarketEODIngressRequest,
    MarketEODIngressRequestV2,
    MarketMinuteIngressRequest,
)
from app.services.delivery_policy import DeliveryExpectation
from app.vocabulary import SOURCE_NAME_PATTERN, normalize_asset_class, normalize_market

ContractKey = tuple[str, int]
ContractModel = (
    type[MarketEODIngressRequest]
    | type[MarketEODIngressRequestV2]
    | type[MarketMinuteIngressRequest]
    | type[FuturesEODIngressRequest]
    | type[IntradayPartitionIngressRequest]
    | type[ExchangeDailyQuoteIngressRequest]
    | type[IndexConstituentsIngressRequest]
    | type[IndexChangesIngressRequest]
)
PositiveStrictInt = Annotated[StrictInt, Field(ge=1)]

_CONTRACT_MODELS: Mapping[ContractKey, ContractModel] = MappingProxyType(
    {
        ("market_eod", 1): MarketEODIngressRequest,
        ("market_eod", 2): MarketEODIngressRequestV2,
        ("market_minute", 1): MarketMinuteIngressRequest,
        ("futures_eod", 1): FuturesEODIngressRequest,
        ("intraday_partition", 1): IntradayPartitionIngressRequest,
        ("exchange_daily_quote", 1): ExchangeDailyQuoteIngressRequest,
        ("index_constituents", 1): IndexConstituentsIngressRequest,
        ("index_constituent_changes", 1): IndexChangesIngressRequest,
    }
)


# Registry and catalog frequency of a dataset that stores each kind of intraday record.
INTRADAY_DATASET_FREQUENCY: Mapping[str, str] = MappingProxyType(
    {"bar_1m": "minute", "tick": "tick"}
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
    market_timezone: Literal["Asia/Taipei"] | None = None
    price_adjustment: Literal["none"] | None = None
    # The one kind of intraday record an ``intraday_partition`` dataset stores.
    record_kind: Literal["bar_1m", "tick"] | None = None

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
    # Explicit provider scope is preferred.  For compatibility with operator
    # policy documents, the ingest service also derives this from the typed
    # schedule/missing-delivery expected_sources fields when omitted.
    allowed_sources: list[str] = Field(default_factory=list, max_length=32)
    delivery_expectation: DeliveryExpectation | None = None

    @model_validator(mode="after")
    def validate_versions(self) -> "DatasetContractDeclaration":
        if any(version < 1 for version in self.accepted_schema_versions):
            raise ValueError("accepted_schema_versions must contain positive integers")
        if len(self.accepted_schema_versions) != len(set(self.accepted_schema_versions)):
            raise ValueError("accepted_schema_versions must not contain duplicates")
        if self.current_schema_version not in self.accepted_schema_versions:
            raise ValueError("current_schema_version must be accepted")
        if self.schema_id == "market_minute" and (
            self.defaults.market_timezone != "Asia/Taipei"
            or self.defaults.price_adjustment != "none"
        ):
            raise ValueError(
                "market_minute defaults require market_timezone=Asia/Taipei and price_adjustment=none"
            )
        if self.schema_id == "intraday_partition" and (
            self.defaults.market_timezone != "Asia/Taipei"
            or self.defaults.price_adjustment != "none"
            or self.defaults.record_kind is None
        ):
            raise ValueError(
                "intraday_partition defaults require market_timezone=Asia/Taipei, "
                "price_adjustment=none and a record_kind"
            )
        if self.schema_id != "intraday_partition" and self.defaults.record_kind is not None:
            raise ValueError("record_kind is only declared by intraday_partition datasets")
        self.allowed_sources = [source.strip().lower() for source in self.allowed_sources]
        if any(
            not source or re.fullmatch(SOURCE_NAME_PATTERN, source) is None
            for source in self.allowed_sources
        ):
            raise ValueError("allowed_sources must contain stable lowercase provider names")
        if len(self.allowed_sources) != len(set(self.allowed_sources)):
            raise ValueError("allowed_sources must not contain duplicates")
        return self

    def provider_scope(self) -> tuple[str, ...]:
        """Return the fail-closed provider allowlist for this declaration."""
        if self.allowed_sources:
            return tuple(self.allowed_sources)
        if self.delivery_expectation is not None:
            schedule = self.delivery_expectation.schedule
            if schedule is not None and schedule.expected_sources:
                return tuple(schedule.expected_sources)
            expected = self.delivery_expectation.missing_delivery.expected_sources
            if expected:
                return tuple(expected)
        return ()


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
    """Return the deterministic request-body shape and semantic contract."""
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
                "fields": (
                    ["symbol", "bar_start_time"]
                    if schema_id == "market_minute"
                    else ["product_code", "contract_code", "trade_date", "session"]
                    if schema_id == "futures_eod"
                    else ["index_code", "symbol"]
                    if schema_id == "index_constituents"
                    else ["effective_date", "symbol", "action"]
                    if schema_id == "index_constituent_changes"
                    else ["symbol", "trade_date"]
                ),
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
    if (schema_id, schema_version) == ("market_eod", 2):
        semantic_rules = [rule for rule in semantic_rules if not rule["id"].startswith("row.ohlc.")]
    if schema_id == "intraday_partition":
        # One described object per request: no rows to bound, order or deduplicate.
        row_rules = (
            "row.ohlc.",
            "batch.sequence.",
            "payload.coverage.",
            "payload.declared_record_count.",
            "payload.delivery_key.",
            "payload.data.max_items",
        )
        semantic_rules = [rule for rule in semantic_rules if not rule["id"].startswith(row_rules)]
        semantic_rules.extend(
            [
                {
                    "id": "partition.single_trading_day",
                    "scope": "payload",
                    "description": "the partition trade_date equals batch.data_date; coverage dates, when present, equal it too",
                    "parameters": {
                        "left": "data[0].trade_date",
                        "operator": "equal",
                        "right": "batch.data_date",
                        "coverage": ["batch.coverage_start_date", "batch.coverage_end_date"],
                    },
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                },
                {
                    "id": "partition.bar_1m.row_count_bound",
                    "scope": "payload.data[0]",
                    "description": "a one-minute bar partition cannot hold more rows than a day has minutes",
                    "parameters": {
                        "when": {"record_kind": "bar_1m"},
                        "left": "row_count",
                        "operator": "less_than_or_equal",
                        "right": MINUTES_PER_DAY,
                    },
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                },
                {
                    "id": "partition.rows.layout",
                    "scope": "payload.data[0]",
                    "description": "the object at rows_ref holds exactly the columns of x-findb-row-layouts[record_kind], in order, for this symbol and trade_date only; the producer maps provider fields before delivery",
                    "parameters": {
                        "layout": "x-findb-row-layouts[record_kind]",
                        "instants": "utc",
                        "trade_date": "exchange_local",
                        "enforcement_stage": "normalization",
                    },
                    "context_dependencies": [],
                    "error_code": None,
                },
                {
                    "id": "partition.record_kind.dataset_declared",
                    "scope": "payload.data[0]",
                    "description": "record_kind must be the kind the dataset declares",
                    "parameters": {
                        "row_field": "record_kind",
                        "dataset_field": "defaults.record_kind",
                        "operator": "equal",
                    },
                    "context_dependencies": [
                        {"kind": "dataset_context", "path": "defaults.record_kind"}
                    ],
                    "error_code": "INGRESS_SCHEMA_NOT_ALLOWED",
                },
            ]
        )
    if schema_id == "exchange_daily_quote":
        # Relations between published values are data-quality rules (see the row model).
        quote_rules = ("row.ohlc.", "batch.sequence.", "payload.coverage.")
        semantic_rules = [rule for rule in semantic_rules if not rule["id"].startswith(quote_rules)]
        semantic_rules.extend(
            [
                {
                    "id": "quote.single_trading_day",
                    "scope": "payload",
                    "description": "every row trade_date equals batch.data_date; coverage dates, when present, equal it too",
                    "parameters": {
                        "left": "data[*].trade_date",
                        "operator": "equal",
                        "right": "batch.data_date",
                        "coverage": ["batch.coverage_start_date", "batch.coverage_end_date"],
                    },
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                },
                {
                    "id": "quote.trading_status.matches_volume",
                    "scope": "payload.data[*]",
                    "description": "trading_status is traded exactly when volume is positive",
                    "parameters": {"traded": "volume > 0", "no_trade": "volume = 0"},
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                },
                {
                    "id": "quote.report.raw_object",
                    "scope": "payload.batch",
                    "description": "source_raw_ref names the gzip of the report; source_raw_sha256 and report.gzip_bytes describe the gzip, report.original_* the report as served",
                    "parameters": {
                        "encoding": "gzip",
                        "enforcement_stage": "normalization",
                    },
                    "context_dependencies": [],
                    "error_code": None,
                },
            ]
        )
    if schema_id in INDEX_REFERENCE_SCHEMAS:
        # One publisher document part per request; its rows carry no prices to bound.
        reference_rules = (
            "row.ohlc.",
            "batch.sequence.",
            "batch.coverage.",
            "batch.backfill.",
            "payload.coverage.",
        )
        semantic_rules = [
            rule for rule in semantic_rules if not rule["id"].startswith(reference_rules)
        ]
        semantic_rules.extend(
            [
                {
                    "id": "reference.document.raw_object",
                    "scope": "payload.batch",
                    "description": "source_raw_ref names the gzip of the publisher document; source_raw_sha256 and document.gzip_bytes describe the gzip, document.original_* the document as served",
                    "parameters": {"encoding": "gzip", "enforcement_stage": "normalization"},
                    "context_dependencies": [],
                    "error_code": None,
                },
                {
                    "id": "reference.document.dates",
                    "scope": "payload.batch",
                    "description": "data_date is the publisher-local observation date; document.document_date, the date the content speaks for, must not follow it",
                    "parameters": {
                        "left": "batch.document.document_date",
                        "operator": "less_than_or_equal",
                        "right": "batch.data_date",
                    },
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                },
                {
                    "id": "reference.indexes.declared_once_with_root_parents",
                    "scope": "payload.batch",
                    "description": "each index is declared once; a parent_code names another index the document lists, and that parent has no parent of its own",
                    "parameters": {"indexes": "batch.indexes[*] | batch.index", "max_depth": 1},
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                },
                {
                    "id": "reference.document.part",
                    "scope": "payload.batch.document",
                    "description": (
                        "a change workbook is delivered one index at a time (part_key is the index code); a constituent report is delivered whole (part_key is document)"
                    ),
                    "parameters": {
                        "kind": INDEX_REFERENCE_SCHEMAS[schema_id],
                        "part_key": "batch.index.code"
                        if schema_id == "index_constituent_changes"
                        else "document",
                    },
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                },
            ]
        )
        if schema_id == "index_constituents":
            semantic_rules.append(
                {
                    "id": "constituents.complete_lists",
                    "scope": "payload",
                    "description": "every row trade_date equals document.document_date, and every declared index lists exactly constituent_count rows",
                    "parameters": {
                        "row_date": "data[*].trade_date",
                        "document_date": "batch.document.document_date",
                        "count": "batch.indexes[*].constituent_count",
                    },
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                }
            )
        if schema_id == "index_constituent_changes":
            semantic_rules.append(
                {
                    "id": "changes.complete_history",
                    "scope": "payload",
                    "description": "history_start equals the earliest effective_date, no effective_date follows document.document_date, ordinals are unique, every row of an effective date states the same constituents_after, and a stated count_change is the signed number of rows of its action on that date",
                    "parameters": {
                        "history_start": "min(data[*].effective_date)",
                        "latest": "batch.document.document_date",
                    },
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                }
            )
    if schema_id == "futures_eod":
        observation_fields = ["open", "high", "low", "close", "settlement_price", "open_interest"]
        schema["$defs"]["FuturesEODRow"]["anyOf"] = [
            {"required": [field], "properties": {field: {"not": {"type": "null"}}}}
            for field in observation_fields
        ] + [
            {
                "required": ["volume"],
                "properties": {"volume": {"type": "integer", "exclusiveMinimum": 0}},
            }
        ]
        semantic_rules.append(
            {
                "id": "futures.row.minimum_observation",
                "scope": "payload.data[*]",
                "description": "every futures row requires a source-provided OHLC, settlement price or open interest (including zero), or positive volume",
                "parameters": {
                    "operator": "any_non_null_or_positive",
                    "non_null_fields": observation_fields,
                    "positive_field": "volume",
                    "exclusive_minimum": 0,
                },
                "context_dependencies": [],
                "error_code": "INGRESS_SCHEMA_INVALID",
            }
        )
        semantic_rules.append(
            {
                "id": "futures.exchange_trade_date_and_session",
                "scope": "payload.data[*]",
                "description": "trade_date is TAIFEX attribution date; expiry and session are distinct contract identities; no midnight inference",
                "parameters": {"market": "TW", "products": ["TX", "MTX", "TMF", "TE", "TF"]},
                "context_dependencies": [],
                "error_code": "INGRESS_SCHEMA_INVALID",
            }
        )
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
    if schema_id == "market_minute":
        semantic_rules.extend(
            [
                {
                    "id": "minute.timestamps.one_minute_and_signal_end",
                    "scope": "payload.data[*]",
                    "description": "timestamps are timezone-aware UTC; end is start plus one minute and signal equals end",
                    "parameters": {
                        "bar_end_time": "bar_start_time + PT1M",
                        "signal_time": "bar_end_time",
                    },
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                },
                {
                    "id": "minute.sequence.identity_and_governance",
                    "scope": "payload.batch",
                    "description": "snapshot, daily update, universe identity, checksum and sequence metadata are required",
                    "parameters": {
                        "delivery_mode": "sequenced_snapshot",
                        "max_symbols": 50,
                        "max_rows": 15000,
                        "canonical_identity": "UTF-8 compact sorted-key JSON object {data_date: ISO-8601 date, dataset_key, sequence: integer, snapshot_id}",
                        "request_key_format": "mmr:{sha256_hex(canonical_identity)}",
                        "idempotency_key_format": "mms:{sha256_hex(canonical_identity)}",
                    },
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                },
                {
                    "id": "minute.status_and_anomaly.references",
                    "scope": "payload",
                    "description": "symbol outcomes and anomalies must reference valid rows; every null volume or turnover has exactly one anomaly and anomalies require null fields",
                    "parameters": {
                        "anomaly_key": ["symbol", "bar_start_time", "field"],
                        "bidirectional_null_fields": ["volume", "turnover"],
                    },
                    "context_dependencies": [],
                    "error_code": "INGRESS_SCHEMA_INVALID",
                },
            ]
        )
    row_string_paths = {
        "futures_eod": [
            "payload.data[*].product_code",
            "payload.data[*].contract_code",
            "payload.data[*].contract_month",
            "payload.data[*].session",
        ],
        "exchange_daily_quote": [
            "payload.data[*].symbol",
            "payload.data[*].name",
            "payload.data[*].currency",
            "payload.batch.report.url",
            "payload.batch.report.original_sha256",
            "payload.batch.report.etag",
        ],
        "index_constituents": [
            "payload.batch.document.url",
            "payload.batch.document.part_key",
            "payload.batch.document.original_sha256",
            "payload.batch.document.etag",
            "payload.batch.indexes[*].code",
            "payload.batch.indexes[*].name_en",
            "payload.batch.indexes[*].name_zh",
            "payload.batch.indexes[*].parent_code",
            "payload.batch.indexes[*].classification.code",
            "payload.batch.indexes[*].classification.name_en",
            "payload.batch.indexes[*].classification.name_zh",
            "payload.data[*].index_code",
            "payload.data[*].symbol",
            "payload.data[*].name_en",
            "payload.data[*].name_zh",
            "payload.data[*].listing_place",
            "payload.data[*].currency",
            "payload.data[*].industry.code",
            "payload.data[*].industry.name_en",
            "payload.data[*].industry.name_zh",
        ],
        "index_constituent_changes": [
            "payload.batch.document.url",
            "payload.batch.document.part_key",
            "payload.batch.document.original_sha256",
            "payload.batch.document.etag",
            "payload.batch.index.code",
            "payload.batch.index.name_en",
            "payload.batch.index.name_zh",
            "payload.batch.index.parent_code",
            "payload.batch.index.classification.code",
            "payload.batch.index.classification.name_en",
            "payload.batch.index.classification.name_zh",
            "payload.data[*].symbol",
            "payload.data[*].name_en",
            "payload.data[*].name_zh",
            "payload.data[*].listing_place",
        ],
        "intraday_partition": [
            "payload.data[*].symbol",
            "payload.data[*].source_symbol",
            "payload.data[*].rows_ref",
            "payload.data[*].rows_sha256",
        ],
        "market_eod": [
            "payload.data[*].symbol",
            "payload.data[*].source_symbol",
            "payload.data[*].name",
            "payload.data[*].currency",
        ],
        "market_minute": [
            "payload.data[*].symbol",
            "payload.data[*].source_symbol",
            "payload.symbol_statuses[*].symbol",
            "payload.symbol_statuses[*].reason",
            "payload.batch.snapshot_id",
            "payload.batch.daily_update_id",
            "payload.batch.universe_id",
            "payload.batch.anomalies[*].symbol",
            "payload.batch.anomalies[*].raw_value",
            "payload.batch.anomalies[*].reason",
        ],
    }[schema_id]
    transformations = [
        {
            "id": "normalization.strings.strip_whitespace",
            "scope": "request_body",
            "description": "leading and trailing whitespace is stripped before validation",
            "operation": "strip_leading_trailing_whitespace",
            "phase": "before_field_validation",
            "paths": [
                "dataset_key",
                "source",
                "request_key",
                "idempotency_key",
                "payload.batch.source_raw_ref",
                "payload.batch.source_raw_sha256",
                *row_string_paths,
            ],
        }
    ]
    contract_scope = {
        "artifact_kind": "versioned_request_body_shape_and_semantics",
        "necessary_for_api_acceptance": True,
        "sufficient_for_api_acceptance": False,
        "dispatch_discriminators": [
            {
                "path": "schema_id",
                "type": "string",
                "matching": "exact",
                "normalization": "none_before_dispatch",
                "expected": schema_id,
            },
            {
                "path": "schema_version",
                "type": "integer_non_boolean",
                "matching": "exact",
                "normalization": "none_before_dispatch",
                "expected": schema_version,
            },
        ],
        "covers": [
            {
                "id": "request_body.json_parsing",
                "stage": "body_parsing",
            },
            {
                "id": "request_body.shape",
                "stage": "json_schema_validation",
            },
            {
                "id": "request_body.normalization",
                "stage": "body_normalization",
                "extension": "x-findb-transformations",
            },
            {
                "id": "request_body.semantics",
                "stage": "body_semantic_validation",
                "extension": "x-findb-semantic-rules",
            },
        ],
        "excludes": [
            "authentication_and_database_credential_lookup",
            "rate_limiting",
            "credential_source_and_dataset_authorization",
            "dataset_registry_state_and_contract_declaration",
            "idempotency_state",
            "infrastructure_availability",
        ],
        "additional_acceptance_boundaries": [
            {
                "id": "authentication.api_key",
                "stage": "before_attempt_dependency",
                "http_statuses": [401, 403],
                "public_codes": [],
                "attempt_semantics": "not_created",
            },
            {
                "id": "authentication.database_lookup",
                "stage": "before_attempt_dependency",
                "http_statuses": [503],
                "public_codes": [],
                "attempt_semantics": "not_created",
            },
            {
                "id": "rate_limit.credential_or_client_ip",
                "stage": "before_attempt_dependency",
                "http_statuses": [429],
                "public_codes": [],
                "attempt_semantics": "not_created",
                "actors": ["source_client", "client_ip"],
            },
            {
                "id": "request.client_ip.available",
                "stage": "before_attempt_dependency",
                "http_statuses": [403],
                "public_codes": [],
                "attempt_semantics": "not_created",
            },
            {
                "id": "credential.source_binding",
                "stage": "after_attempt_application_validation",
                "http_statuses": [403],
                "public_codes": ["SOURCE_IDENTITY_MISMATCH"],
                "attempt_semantics": "durably_rejected",
            },
            {
                "id": "credential.dataset_allowlist",
                "stage": "after_attempt_application_validation",
                "http_statuses": [403],
                "public_codes": ["DATASET_ACCESS_DENIED"],
                "attempt_semantics": "durably_rejected",
            },
            {
                "id": "dataset.existence",
                "stage": "after_attempt_application_validation",
                "http_statuses": [400],
                "public_codes": ["DATASET_NOT_FOUND"],
                "attempt_semantics": "durably_rejected",
            },
            {
                "id": "dataset.active",
                "stage": "after_attempt_application_validation",
                "http_statuses": [409],
                "public_codes": ["DATASET_INACTIVE"],
                "attempt_semantics": "durably_rejected",
            },
            {
                "id": "dataset.contract_declaration",
                "stage": "after_attempt_application_validation",
                "http_statuses": [409],
                "public_codes": ["DATASET_CONTRACT_NOT_CONFIGURED"],
                "attempt_semantics": "durably_rejected",
            },
            {
                "id": "dataset.contract_scope",
                "stage": "after_attempt_application_validation",
                "http_statuses": [409],
                "public_codes": ["DATASET_CONTRACT_NOT_CONFIGURED"],
                "attempt_semantics": "durably_rejected",
            },
            {
                "id": "dataset.accepted_contract_version",
                "stage": "after_attempt_application_validation",
                "http_statuses": [422],
                "public_codes": ["INGRESS_SCHEMA_NOT_ALLOWED"],
                "attempt_semantics": "durably_rejected",
            },
            {
                "id": "idempotency.collision",
                "stage": "after_attempt_persistence_validation",
                "http_statuses": [409],
                "public_codes": ["IDEMPOTENCY_PAYLOAD_MISMATCH"],
                "attempt_semantics": "durably_rejected",
            },
            {
                "id": "infrastructure.database_or_internal_failure",
                "stage": "multiple",
                "http_statuses": [500, 503],
                "public_codes": ["DATABASE_UNAVAILABLE", "INTERNAL_ERROR"],
                "attempt_semantics": "depends_on_failure_stage",
            },
        ],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:findb:ingress-contract:{schema_id}:v{schema_version}",
        "x-findb-contract": {
            "schema_id": schema_id,
            "schema_version": schema_version,
        },
        "x-findb-contract-scope": contract_scope,
        "x-findb-transformations": transformations,
        "x-findb-semantic-rules": semantic_rules,
        **(
            {
                "x-findb-row-layouts": {
                    kind: {"format": "parquet", "columns": [column._asdict() for column in columns]}
                    for kind, columns in INTRADAY_ROW_LAYOUTS.items()
                }
            }
            if schema_id == "intraday_partition"
            else {}
        ),
        **schema,
    }


def validate_ingress_request(value: Mapping[str, Any]) -> IngressRequest:
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
            "Dataset contract defaults.asset_class does not match dataset_registry.asset_class"
        )


def validate_request_currency(
    declaration: DatasetContractDeclaration,
    request: IngressRequest,
) -> None:
    """Apply dataset-dependent currency requirements before persistence."""
    if declaration.defaults.currency is not None:
        return
    if isinstance(request, (MarketEODIngressRequest, MarketEODIngressRequestV2)) and any(
        row.currency is None for row in request.payload.data
    ):
        raise CurrencyRequiredError(
            "currency is required for every market_eod row when the dataset has no default currency"
        )


def request_record_kind_allowed(
    declaration: DatasetContractDeclaration,
    request: IngressRequest,
) -> bool:
    """Whether the dataset stores the kind of intraday record this request delivers."""
    return not isinstance(request, IntradayPartitionIngressRequest) or all(
        row.record_kind == declaration.defaults.record_kind for row in request.payload.data
    )
