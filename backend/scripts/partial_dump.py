"""Export partial dataset from remote database using YAML configuration."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.schemas.partial_dump import PartialDumpConfig, TableConfig, load_partial_dump_config

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_CONFIG_PATH = BACKEND_ROOT / "configs" / "partial_dump.yaml"


@dataclass
class TableExportResult:
    schema: str
    name: str
    status: str
    row_count: int | None = None
    file: str | None = None
    reason: str | None = None


@dataclass
class TableColumn:
    name: str
    data_type: str
    is_nullable: bool
    udt_name: str | None
    char_max_length: int | None
    numeric_precision: int | None
    numeric_scale: int | None
    column_default: str | None


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _qualified_table(schema: str, name: str) -> str:
    return f"{_quote_ident(schema)}.{_quote_ident(name)}"


def _normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def _serialize_value(value: Any) -> str | int | float | bool | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def _table_key(schema: str, name: str) -> str:
    return f"{schema}.{name}"


async def _discover_tables(
    conn: AsyncConnection, config: PartialDumpConfig
) -> list[tuple[str, str]]:
    if not config.table_discovery.enabled:
        return []

    include_schemas = set(config.table_discovery.include_schemas)
    exclude_tables = set(config.table_discovery.exclude_tables)
    result = await conn.execute(text("""
            SELECT n.nspname AS table_schema, c.relname AS table_name
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
              AND NOT c.relispartition
            ORDER BY n.nspname, c.relname
            """))

    tables: list[tuple[str, str]] = []
    for row in result:
        schema = row.table_schema
        name = row.table_name
        if schema not in include_schemas:
            continue
        if f"{schema}.{name}" in exclude_tables:
            continue
        tables.append((schema, name))
    return tables


def _default_table_config(config: PartialDumpConfig, schema: str, name: str) -> TableConfig:
    return TableConfig(
        table_schema=schema,
        name=name,
        enabled=True,
        mode=config.defaults.mode,
        where_sql=config.defaults.where_sql,
        params=dict(config.defaults.params),
        order_by=config.defaults.order_by,
        limit=config.defaults.limit,
    )


def _resolve_tables(
    config: PartialDumpConfig,
    discovered_tables: list[tuple[str, str]],
) -> list[tuple[TableConfig, bool]]:
    resolved: list[tuple[TableConfig, bool]] = []
    overrides = {(table.table_schema, table.name): table for table in config.tables}
    seen: set[tuple[str, str]] = set()

    if config.table_discovery.enabled:
        for schema, name in discovered_tables:
            key = (schema, name)
            override = overrides.get(key)
            if override is not None:
                seen.add(key)
                if override.enabled:
                    resolved.append((override, True))
            else:
                resolved.append((_default_table_config(config, schema, name), False))
                seen.add(key)

    for table in config.tables:
        key = (table.table_schema, table.name)
        if key in seen or not table.enabled:
            continue
        resolved.append((table, True))

    return resolved


async def _load_table_columns(conn: AsyncConnection, schema: str, table: str) -> list[TableColumn]:
    result = await conn.execute(
        text("""
            SELECT
                column_name,
                data_type,
                is_nullable,
                udt_name,
                character_maximum_length,
                numeric_precision,
                numeric_scale,
                column_default
            FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = :table
            ORDER BY ordinal_position
            """),
        {"schema": schema, "table": table},
    )

    return [
        TableColumn(
            name=row.column_name,
            data_type=row.data_type,
            is_nullable=row.is_nullable == "YES",
            udt_name=row.udt_name,
            char_max_length=row.character_maximum_length,
            numeric_precision=row.numeric_precision,
            numeric_scale=row.numeric_scale,
            column_default=row.column_default,
        )
        for row in result
    ]


async def _sample_instrument_ids(conn: AsyncConnection, sample_size: int) -> list[str]:
    try:
        result = await conn.execute(
            text("""
                SELECT instrument_id::text
                FROM (
                    SELECT
                        instrument_id,
                        market,
                        ROW_NUMBER() OVER (PARTITION BY market ORDER BY instrument_id) AS rank_in_market
                    FROM public.instruments
                ) ranked
                WHERE rank_in_market <= :sample_size
                ORDER BY instrument_id
                """),
            {"sample_size": sample_size},
        )
        return [row.instrument_id for row in result]
    except Exception:
        return []


def _resolve_export_columns(table: TableConfig, columns: list[TableColumn]) -> list[str]:
    all_columns = [column.name for column in columns]
    if table.columns_include:
        include_set = set(table.columns_include)
        return [name for name in all_columns if name in include_set]

    if table.columns_exclude:
        exclude_set = set(table.columns_exclude)
        return [name for name in all_columns if name not in exclude_set]

    return all_columns


def _pick_time_column(config: PartialDumpConfig, columns: list[str]) -> str | None:
    for column in config.selection.time_columns_priority:
        if column in columns:
            return column
    return None


def _build_query(
    config: PartialDumpConfig,
    table: TableConfig,
    explicit_override: bool,
    selected_columns: list[str],
    sampled_instrument_ids: list[str],
) -> tuple[str | None, dict[str, Any], str | None]:
    mode = table.mode or config.defaults.mode
    where_clauses: list[str] = []
    params: dict[str, Any] = {}

    if mode == "where" and table.where_sql:
        where_clauses.append(f"({table.where_sql})")
        params.update(table.params)
    elif mode == "latest_n_days":
        if not table.latest_by_column or table.latest_n_days is None:
            return None, {}, "latest_n_days requires latest_by_column/latest_n_days"
        if table.latest_by_column not in selected_columns:
            return (
                None,
                {},
                f"latest_by_column '{table.latest_by_column}' not found in table columns",
            )
        cutoff = datetime.now(UTC) - timedelta(days=table.latest_n_days)
        where_clauses.append(f"{_quote_ident(table.latest_by_column)} >= :_latest_cutoff")
        params["_latest_cutoff"] = cutoff
    elif mode == "sample":
        return None, {}, "mode=sample is not supported yet; use where/latest_n_days"

    # Auto selection only for discovery tables without explicit override.
    if not explicit_override:
        if table.where_sql and mode == "where":
            pass

        time_column = _pick_time_column(config, selected_columns)
        has_instrument_id = "instrument_id" in selected_columns

        if has_instrument_id:
            if sampled_instrument_ids:
                where_clauses.append('"instrument_id" IN :_instrument_ids')
                params["_instrument_ids"] = sampled_instrument_ids
            else:
                return (
                    None,
                    {},
                    "cannot sample instrument_id because public.instruments is unavailable",
                )

        if time_column is not None:
            cutoff = datetime.now(UTC) - timedelta(days=config.selection.window_days)
            where_clauses.append(f"{_quote_ident(time_column)} >= :_window_cutoff")
            params["_window_cutoff"] = cutoff

        if not has_instrument_id and time_column is None:
            policy = config.selection.unresolved_table_policy
            if policy in {"discuss", "skip"}:
                return (
                    None,
                    {},
                    "no instrument_id/time column; unresolved_table_policy requests skip",
                )

    select_columns = ", ".join(_quote_ident(column) for column in selected_columns)
    sql = f"SELECT {select_columns} FROM {_qualified_table(table.table_schema, table.name)}"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)

    if table.order_by:
        sql += f" ORDER BY {table.order_by}"

    if table.limit is not None:
        sql += f" LIMIT {table.limit}"

    return sql, params, None


def _build_statement(sql: str, params: dict[str, Any]):
    statement = text(sql)
    if "_instrument_ids" in params:
        statement = statement.bindparams(bindparam("_instrument_ids", expanding=True))
    return statement


def _artifact_dir(config: PartialDumpConfig, output_dir: str | None = None) -> Path:
    utc_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    artifact_name = config.target.artifact_name.format(profile=config.profile, utc_ts=utc_timestamp)
    root = Path(output_dir) if output_dir else Path(config.target.output_dir)
    if not root.is_absolute():
        root = BACKEND_ROOT / root
    return root / artifact_name


def _to_pg_type(column: TableColumn) -> str:
    data_type = column.data_type
    if data_type == "ARRAY":
        udt = column.udt_name or "text"
        base = udt[1:] if udt.startswith("_") else udt
        return f"{base}[]"
    if data_type in {"character varying", "character"} and column.char_max_length is not None:
        return f"{data_type}({column.char_max_length})"
    if data_type == "numeric" and column.numeric_precision is not None:
        if column.numeric_scale is None:
            return f"numeric({column.numeric_precision})"
        return f"numeric({column.numeric_precision},{column.numeric_scale})"
    if data_type == "USER-DEFINED" and column.udt_name:
        return column.udt_name
    return data_type


def _write_schema_sql(
    path: Path,
    table_columns: dict[str, list[TableColumn]],
) -> None:
    lines: list[str] = [
        "-- Best-effort schema snapshot generated by scripts/partial_dump.py",
        "-- For exact DDL, use pg_dump --schema-only",
        "",
    ]

    for table_key in sorted(table_columns.keys()):
        schema, name = table_key.split(".", 1)
        columns = table_columns[table_key]
        lines.append(f"CREATE TABLE IF NOT EXISTS {_qualified_table(schema, name)} (")
        for index, column in enumerate(columns):
            nullable = "" if column.is_nullable else " NOT NULL"
            default_sql = f" DEFAULT {column.column_default}" if column.column_default else ""
            suffix = "," if index < len(columns) - 1 else ""
            lines.append(
                f"    {_quote_ident(column.name)} {_to_pg_type(column)}{default_sql}{nullable}{suffix}"
            )
        lines.append(");")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _write_load_sql(
    path: Path, config: PartialDumpConfig, exported_tables: list[dict[str, Any]]
) -> None:
    lines = [
        "-- psql load script generated by scripts/partial_dump.py",
        '-- Run: psql "$DATABASE_URL" -f load.sql',
    ]
    if config.load.on_conflict != "error":
        lines.append(
            f"-- NOTE: on_conflict={config.load.on_conflict}. This script uses direct \\copy and does not implement merge semantics."
        )

    lines.append("BEGIN;")
    lines.append("")
    if config.load.disable_triggers:
        lines.append("-- disable trigger/FK checks during load (local development use only)")
        lines.append("SET session_replication_role = replica;")
        lines.append("")

    if config.load.truncate_before_load:
        for entry in exported_tables:
            lines.append(
                f"TRUNCATE TABLE {_qualified_table(entry['schema'], entry['name'])} CASCADE;"
            )
        lines.append("")

    for entry in exported_tables:
        qualified = _qualified_table(entry["schema"], entry["name"])
        column_list = ", ".join(_quote_ident(column) for column in entry["columns"])
        csv_path = entry["file"].replace("\\", "/")
        lines.append(
            f"\\copy {qualified} ({column_list}) FROM '{csv_path}' WITH (FORMAT csv, HEADER true)"
        )

    lines.append("")
    if config.load.disable_triggers:
        lines.append("SET session_replication_role = origin;")
        lines.append("")
    lines.append("COMMIT;")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


async def _load_fk_edges(conn: AsyncConnection) -> list[tuple[str, str]]:
    result = await conn.execute(text("""
            SELECT
                child_ns.nspname AS child_schema,
                child_cls.relname AS child_table,
                parent_ns.nspname AS parent_schema,
                parent_cls.relname AS parent_table
            FROM pg_constraint con
            JOIN pg_class child_cls ON child_cls.oid = con.conrelid
            JOIN pg_namespace child_ns ON child_ns.oid = child_cls.relnamespace
            JOIN pg_class parent_cls ON parent_cls.oid = con.confrelid
            JOIN pg_namespace parent_ns ON parent_ns.oid = parent_cls.relnamespace
            WHERE con.contype = 'f'
            """))
    return [
        (
            _table_key(row.child_schema, row.child_table),
            _table_key(row.parent_schema, row.parent_table),
        )
        for row in result
    ]


def _sort_tables_by_fk(
    exported_tables: list[dict[str, Any]],
    fk_edges: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    if not exported_tables:
        return exported_tables

    key_to_item = {_table_key(item["schema"], item["name"]): item for item in exported_tables}
    original_order = {
        _table_key(item["schema"], item["name"]): index
        for index, item in enumerate(exported_tables)
    }
    node_keys = set(key_to_item.keys())

    dependencies: dict[str, set[str]] = {key: set() for key in node_keys}
    reverse_deps: dict[str, set[str]] = {key: set() for key in node_keys}
    for child_key, parent_key in fk_edges:
        if child_key not in node_keys or parent_key not in node_keys or child_key == parent_key:
            continue
        if parent_key in dependencies[child_key]:
            continue
        dependencies[child_key].add(parent_key)
        reverse_deps[parent_key].add(child_key)

    indegree = {key: len(parent_keys) for key, parent_keys in dependencies.items()}
    ready = sorted(
        [key for key, degree in indegree.items() if degree == 0],
        key=lambda key: original_order[key],
    )

    ordered_keys: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered_keys.append(current)
        for child in sorted(reverse_deps[current], key=lambda key: original_order[key]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=lambda key: original_order[key])

    if len(ordered_keys) < len(node_keys):
        remaining = sorted(
            [key for key in node_keys if key not in ordered_keys],
            key=lambda key: original_order[key],
        )
        ordered_keys.extend(remaining)

    return [key_to_item[key] for key in ordered_keys]


async def _run_dump(
    config: PartialDumpConfig, config_path: Path, output_dir: str | None, dry_run: bool
) -> int:
    database_url = _normalize_database_url(os.environ[config.source.database_url_env])
    engine = create_async_engine(database_url, echo=False)

    artifact_dir = _artifact_dir(config, output_dir)
    tables_dir = artifact_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "profile": config.profile,
        "config_path": str(config_path),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "results": [],
    }

    exported_tables_for_sql: list[dict[str, Any]] = []
    exported_table_columns: dict[str, list[TableColumn]] = {}
    fk_edges: list[tuple[str, str]] = []

    try:
        async with engine.connect() as conn:
            if config.source.statement_timeout_ms > 0:
                # asyncpg does not support bind placeholders in `SET ... = ...`.
                # Use set_config() with a bound text value instead.
                await conn.execute(
                    text("SELECT set_config('statement_timeout', :statement_timeout, false)"),
                    {"statement_timeout": f"{config.source.statement_timeout_ms}ms"},
                )

            discovered_tables = await _discover_tables(conn, config)
            resolved_tables = _resolve_tables(config, discovered_tables)
            sampled_ids = await _sample_instrument_ids(
                conn, config.selection.instrument_sample_size
            )
            fk_edges = await _load_fk_edges(conn)

            for table, explicit_override in resolved_tables:
                key = f"{table.table_schema}.{table.name}"
                columns = await _load_table_columns(conn, table.table_schema, table.name)
                if not columns:
                    manifest["results"].append(
                        TableExportResult(
                            schema=table.table_schema,
                            name=table.name,
                            status="skipped",
                            reason="table not found or no columns",
                        ).__dict__
                    )
                    continue

                selected_columns = _resolve_export_columns(table, columns)
                if not selected_columns:
                    manifest["results"].append(
                        TableExportResult(
                            schema=table.table_schema,
                            name=table.name,
                            status="skipped",
                            reason="no columns left after include/exclude",
                        ).__dict__
                    )
                    continue

                sql, params, skip_reason = _build_query(
                    config=config,
                    table=table,
                    explicit_override=explicit_override,
                    selected_columns=selected_columns,
                    sampled_instrument_ids=sampled_ids,
                )
                if skip_reason or sql is None:
                    manifest["results"].append(
                        TableExportResult(
                            schema=table.table_schema,
                            name=table.name,
                            status="skipped",
                            reason=skip_reason,
                        ).__dict__
                    )
                    continue

                if dry_run:
                    manifest["results"].append(
                        TableExportResult(
                            schema=table.table_schema,
                            name=table.name,
                            status="planned",
                        ).__dict__
                    )
                    continue

                statement = _build_statement(sql, params)
                result = await conn.execute(statement, params)
                rows = result.fetchall()

                file_name = f"{table.table_schema}.{table.name}.csv"
                file_path = tables_dir / file_name
                with file_path.open("w", encoding="utf-8", newline="") as file:
                    writer = csv.writer(file)
                    writer.writerow(selected_columns)
                    for row in rows:
                        writer.writerow(
                            [_serialize_value(row._mapping[column]) for column in selected_columns]
                        )

                row_count = len(rows)
                manifest["results"].append(
                    TableExportResult(
                        schema=table.table_schema,
                        name=table.name,
                        status="exported",
                        row_count=row_count,
                        file=f"tables/{file_name}",
                    ).__dict__
                )
                exported_tables_for_sql.append(
                    {
                        "schema": table.table_schema,
                        "name": table.name,
                        "columns": selected_columns,
                        "file": f"tables/{file_name}",
                    }
                )
                exported_table_columns[key] = columns

        if config.target.include_schema_sql and not dry_run and exported_table_columns:
            _write_schema_sql(artifact_dir / "schema.sql", exported_table_columns)

        if config.target.include_load_sql and not dry_run and exported_tables_for_sql:
            sorted_for_load = _sort_tables_by_fk(exported_tables_for_sql, fk_edges)
            _write_load_sql(artifact_dir / "load.sql", config, sorted_for_load)

        if config.target.include_manifest:
            (artifact_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        summary = defaultdict(int)
        for item in manifest["results"]:
            summary[item["status"]] += 1
        print(f"Artifact: {artifact_dir}")
        print(
            "Results: "
            + ", ".join(f"{status}={count}" for status, count in sorted(summary.items()))
        )

        return 0
    finally:
        await engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Partial dump utility")
    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser("validate", help="Validate partial_dump.yaml")
    validate_parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to partial dump config file",
    )
    validate_parser.add_argument(
        "--no-require-env",
        action="store_true",
        help="Skip checking source.database_url_env existence",
    )

    dump_parser = subparsers.add_parser("dump", help="Run partial dump export")
    dump_parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to partial dump config file",
    )
    dump_parser.add_argument(
        "--output-dir",
        default=None,
        help="Override output root directory",
    )
    dump_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate plan/manifest only, skip querying data",
    )

    return parser


def _cmd_validate(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    config = load_partial_dump_config(config_path, require_env=not args.no_require_env)
    print(f"Config valid: {config_path}")
    print(f"Profile: {config.profile}")
    print(f"Discovery enabled: {config.table_discovery.enabled}")
    print(f"Table overrides: {len(config.tables)}")
    return 0


def _cmd_dump(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    config = load_partial_dump_config(config_path, require_env=True)
    return asyncio.run(
        _run_dump(
            config=config,
            config_path=config_path,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )
    )


def main() -> int:
    # Keep behavior aligned with app config loading from local .env
    load_dotenv(REPO_ROOT / ".env")

    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "validate":
            return _cmd_validate(args)
        if args.command == "dump":
            return _cmd_dump(args)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc!r}")
        traceback.print_exc()
        return 2

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
