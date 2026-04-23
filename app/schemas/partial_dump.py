"""Schema contract and loader for partial dump configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TableMode = Literal["full", "where", "latest_n_days", "sample"]
IsolationLevel = Literal["read_committed", "repeatable_read"]
InstrumentScope = Literal["by_market", "by_table", "global"]
UnresolvedTablePolicy = Literal["discuss", "skip", "full"]
CompressionType = Literal["none", "gzip", "zstd"]
OutputFormat = Literal["csv"]
AnonymizationType = Literal[
    "nullify",
    "static",
    "hash_sha256",
    "mask_email",
    "mask_phone",
]
OnConflictMode = Literal["error", "do_nothing", "upsert"]


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url_env: str
    require_ssl: bool = True
    statement_timeout_ms: int = 120000
    application_name: str = "findb-partial-dump"
    readonly_required: bool = True


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_dir: str
    artifact_name: str = "{profile}_{utc_ts}"
    format: OutputFormat = "csv"
    compress: CompressionType = "zstd"
    include_schema_sql: bool = True
    include_load_sql: bool = True
    include_manifest: bool = True


class SnapshotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    isolation_level: IsolationLevel = "repeatable_read"
    lock_timeout_ms: int = 5000
    idle_in_transaction_timeout_ms: int = 60000


class DefaultsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: TableMode = "where"
    where_sql: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    order_by: str | None = None
    limit: int | None = None
    chunk_size: int = 50000


class SelectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_days: int = 365
    instrument_sample_size: int = 3
    instrument_scope: InstrumentScope = "by_market"
    non_instrument_table_strategy: str = "time_window"
    time_columns_priority: list[str] = Field(
        default_factory=lambda: ["trade_date", "obs_date", "created_at", "updated_at"]
    )
    unresolved_table_policy: UnresolvedTablePolicy = "discuss"


class TableDiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    include_schemas: list[str] = Field(default_factory=lambda: ["public"])
    exclude_tables: list[str] = Field(default_factory=list)
    require_pk_for_upsert: bool = True


class TableAnonymizeRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str
    rule: str
    nullable_fallback: bool = False


class TableConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    table_schema: str = Field(default="public", alias="schema")
    enabled: bool = True
    mode: TableMode | None = None
    where_sql: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    order_by: str | None = None
    limit: int | None = None
    latest_by_column: str | None = None
    latest_n_days: int | None = None
    columns_include: list[str] = Field(default_factory=list)
    columns_exclude: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    merge_keys: list[str] = Field(default_factory=list)
    anonymize: list[TableAnonymizeRule] = Field(default_factory=list)
    post_sql: list[str] = Field(default_factory=list)

    @property
    def fq_name(self) -> str:
        return f"{self.table_schema}.{self.name}"


class AnonymizationRuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: AnonymizationType
    value: str | None = None
    salt_env: str | None = None


class LoadConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    truncate_before_load: bool = True
    disable_triggers: bool = False
    verify_fk_after_load: bool = True
    on_conflict: OnConflictMode = "upsert"
    parallel_jobs: int = 1


class TableCheckConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str
    min_rows: int | None = None
    max_rows: int | None = None
    sql_assert: str | None = None


class ValidationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_count_tolerance_pct: float = 0.0
    required_tables: list[str] = Field(default_factory=list)
    table_checks: list[TableCheckConfig] = Field(default_factory=list)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retries: int = 2
    retry_backoff_ms: int = 1000
    log_level: Literal["debug", "info", "warn"] = "info"
    fail_fast: bool = True


class MetadataConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: str | None = None
    ticket: str | None = None
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None


class PartialDumpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    profile: str
    source: SourceConfig
    target: TargetConfig
    snapshot: SnapshotConfig = Field(default_factory=SnapshotConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    table_discovery: TableDiscoveryConfig = Field(default_factory=TableDiscoveryConfig)
    tables: list[TableConfig]
    anonymization_rules: dict[str, AnonymizationRuleConfig] = Field(default_factory=dict)
    load: LoadConfig = Field(default_factory=LoadConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if value != "1":
            raise ValueError("version must be '1'")
        return value

    @model_validator(mode="after")
    def validate_constraints(self) -> "PartialDumpConfig":
        enabled_tables = [table for table in self.tables if table.enabled]
        if not enabled_tables and not self.table_discovery.enabled:
            raise ValueError(
                "tables must include at least one enabled entry when table_discovery is disabled"
            )

        if self.selection.window_days > 365:
            raise ValueError("selection.window_days must be <= 365")

        known_rules = set(self.anonymization_rules.keys())
        for table in enabled_tables:
            if table.columns_include and table.columns_exclude:
                raise ValueError(
                    f"{table.fq_name}: columns_include and columns_exclude cannot both be non-empty"
                )
            if table.mode == "latest_n_days" and (
                not table.latest_by_column or table.latest_n_days is None
            ):
                raise ValueError(
                    f"{table.fq_name}: latest_by_column and latest_n_days are required for mode=latest_n_days"
                )
            for rule in table.anonymize:
                if rule.rule not in known_rules:
                    raise ValueError(
                        f"{table.fq_name}: anonymize rule '{rule.rule}' is not defined in anonymization_rules"
                    )

        if self.load.parallel_jobs < 1:
            raise ValueError("load.parallel_jobs must be >= 1")

        self._validate_raw_market_payload_limit(enabled_tables)
        return self

    def _validate_raw_market_payload_limit(self, enabled_tables: list[TableConfig]) -> None:
        raw_override = next(
            (
                table
                for table in enabled_tables
                if table.table_schema == "raw" and table.name == "market_payload"
            ),
            None,
        )
        if raw_override:
            if raw_override.limit is None:
                raise ValueError("raw.market_payload must set explicit limit <= 1000")
            if raw_override.limit > 1000:
                raise ValueError("raw.market_payload limit must be <= 1000")
            return

        includes_raw_schema = "raw" in self.table_discovery.include_schemas
        excludes_raw_table = "raw.market_payload" in self.table_discovery.exclude_tables
        if self.table_discovery.enabled and includes_raw_schema and not excludes_raw_table:
            raise ValueError(
                "raw.market_payload is included by table_discovery; add explicit tables override with limit <= 1000"
            )

    def ensure_source_env_present(self) -> None:
        self.resolve_source_database_url()

    def resolve_source_database_url(self) -> tuple[str, str]:
        env_name = self.source.database_url_env
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            return env_name, env_value

        if env_name != "DATABASE_URL":
            fallback_name = "DATABASE_URL"
            fallback_value = os.getenv(fallback_name, "").strip()
            if fallback_value:
                return fallback_name, fallback_value

        raise ValueError(
            (
                f"environment variable '{env_name}' is required and cannot be empty"
                " (or set DATABASE_URL for local fallback)"
            )
        )


def load_partial_dump_config(path: Path, require_env: bool = True) -> PartialDumpConfig:
    """Load and validate partial dump YAML config from disk."""

    with path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)

    if not isinstance(raw, dict):
        raise ValueError("partial dump config must be a mapping object")

    config = PartialDumpConfig.model_validate(raw)
    if require_env:
        config.ensure_source_env_present()
    return config
