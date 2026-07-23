"""Upsert seed data artifact into a running PostgreSQL database."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import MetaData, Table, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, JSON, JSONB, UUID, insert
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.sql import sqltypes

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_PARTIAL_DUMP_ROOT = BACKEND_ROOT / "seed" / "partial_dump"
PROTECTED_TABLES = {"public.alembic_version"}


@dataclass
class ArtifactTable:
    schema: str
    name: str
    file_path: Path

    @property
    def key(self) -> str:
        return f"{self.schema}.{self.name}"


def _normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _qualified_table(schema: str, table: str) -> str:
    return f"{_quote_ident(schema)}.{_quote_ident(table)}"


def _coerce_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"invalid boolean literal: {value!r}")


def _coerce_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized)


def _coerce_array(value: str) -> list[Any]:
    normalized = value.strip()
    if not normalized:
        return []
    if normalized.startswith("{") and normalized.endswith("}"):
        # PostgreSQL array literal to JSON-like list for simple scalar arrays.
        inner = normalized[1:-1]
        if not inner:
            return []
        return [item for item in inner.split(",")]
    parsed = json.loads(normalized)
    if isinstance(parsed, list):
        return parsed
    raise ValueError(f"invalid array literal: {value!r}")


def parse_cell(raw: str, column_type: Any) -> Any:
    """Parse CSV string cell into a Python value that matches SQLAlchemy column type."""

    if raw == "":
        return None

    if isinstance(column_type, (JSON, JSONB)):
        return json.loads(raw)
    if isinstance(column_type, ARRAY):
        return _coerce_array(raw)
    if isinstance(column_type, UUID):
        return uuid.UUID(raw)

    if isinstance(column_type, sqltypes.Boolean):
        return _coerce_bool(raw)
    if isinstance(column_type, (sqltypes.Integer, sqltypes.BigInteger, sqltypes.SmallInteger)):
        return int(raw)
    if isinstance(column_type, (sqltypes.Numeric, sqltypes.DECIMAL)):
        return Decimal(raw)
    if isinstance(column_type, sqltypes.Float):
        return float(raw)
    if isinstance(column_type, sqltypes.DateTime):
        return _coerce_datetime(raw)
    if isinstance(column_type, sqltypes.Date):
        return date.fromisoformat(raw)
    if isinstance(column_type, sqltypes.Time):
        return time.fromisoformat(raw)

    return raw


def _find_latest_artifact(partial_dump_root: Path) -> Path:
    if not partial_dump_root.exists():
        raise FileNotFoundError(f"partial dump root does not exist: {partial_dump_root}")

    candidates = [path for path in partial_dump_root.iterdir() if path.is_dir()]
    if not candidates:
        raise FileNotFoundError(f"no artifact directories found under {partial_dump_root}")

    for candidate in sorted(candidates, key=lambda path: path.name, reverse=True):
        if _artifact_has_exported_tables(candidate):
            return candidate

    raise ValueError(
        f"no usable artifact found under {partial_dump_root}; "
        "run `partial-dump-run` (not --dry-run) to generate exported table files"
    )


def _artifact_has_exported_tables(artifact_dir: Path) -> bool:
    manifest_path = artifact_dir / "manifest.json"
    if not manifest_path.exists():
        return False

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False

    for item in manifest.get("results", []):
        if item.get("status") != "exported":
            continue
        file_rel = item.get("file")
        if not file_rel:
            continue
        if (artifact_dir / file_rel).exists():
            return True
    return False


def _load_manifest(artifact_dir: Path) -> dict[str, Any]:
    manifest_path = artifact_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")

    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _load_artifact_tables(artifact_dir: Path, manifest: dict[str, Any]) -> list[ArtifactTable]:
    tables: list[ArtifactTable] = []
    for item in manifest.get("results", []):
        if item.get("status") != "exported":
            continue
        file_rel = item.get("file")
        if not file_rel:
            continue
        file_path = artifact_dir / file_rel
        if not file_path.exists():
            raise FileNotFoundError(f"artifact file not found: {file_path}")
        tables.append(
            ArtifactTable(
                schema=item["schema"],
                name=item["name"],
                file_path=file_path,
            )
        )

    if not tables:
        raise ValueError(
            "manifest has no exported table files (artifact may be generated by --dry-run)"
        )

    return tables


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
            f"{row.child_schema}.{row.child_table}",
            f"{row.parent_schema}.{row.parent_table}",
        )
        for row in result
    ]


async def _load_existing_tables(conn: AsyncConnection, schemas: set[str]) -> set[str]:
    if not schemas:
        return set()

    stmt = text("""
        SELECT n.nspname AS table_schema, c.relname AS table_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p')
          AND NOT c.relispartition
          AND n.nspname IN :schemas
    """).bindparams(bindparam("schemas", expanding=True))

    result = await conn.execute(stmt, {"schemas": sorted(schemas)})
    return {f"{row.table_schema}.{row.table_name}" for row in result}


async def _truncate_and_cleanup(conn: AsyncConnection, tables: list[ArtifactTable]) -> None:
    target_table_keys = {table.key for table in tables}
    managed_schemas = {table.schema for table in tables}

    existing_tables = await _load_existing_tables(conn, managed_schemas)
    missing_tables = sorted(target_table_keys - existing_tables)
    if missing_tables:
        missing_preview = ", ".join(missing_tables[:10])
        suffix = " ..." if len(missing_tables) > 10 else ""
        raise ValueError(
            "target database is missing required tables in artifact: "
            f"{missing_preview}{suffix}. Run alembic upgrade first."
        )

    obsolete_tables = sorted(existing_tables - target_table_keys - PROTECTED_TABLES)
    for table_key in obsolete_tables:
        schema, table = table_key.split(".", 1)
        await conn.execute(text(f"DROP TABLE IF EXISTS {_qualified_table(schema, table)} CASCADE"))
        print(f"dropped obsolete table: {table_key}")

    truncate_sql = ", ".join(
        _qualified_table(table.schema, table.name)
        for table in sorted(tables, key=lambda item: item.key)
    )
    await conn.execute(text(f"TRUNCATE TABLE {truncate_sql} RESTART IDENTITY CASCADE"))
    print(
        "truncate complete: "
        f"tables={len(target_table_keys)}, dropped_obsolete={len(obsolete_tables)}"
    )


def _sort_tables_by_fk(
    tables: list[ArtifactTable],
    fk_edges: list[tuple[str, str]],
) -> list[ArtifactTable]:
    key_to_table = {table.key: table for table in tables}
    original_order = {table.key: index for index, table in enumerate(tables)}
    table_keys = set(key_to_table.keys())

    dependencies: dict[str, set[str]] = {key: set() for key in table_keys}
    reverse_deps: dict[str, set[str]] = {key: set() for key in table_keys}

    for child_key, parent_key in fk_edges:
        if child_key not in table_keys or parent_key not in table_keys or child_key == parent_key:
            continue
        if parent_key in dependencies[child_key]:
            continue
        dependencies[child_key].add(parent_key)
        reverse_deps[parent_key].add(child_key)

    indegree = {key: len(deps) for key, deps in dependencies.items()}
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

    if len(ordered_keys) < len(table_keys):
        remaining = sorted(
            [key for key in table_keys if key not in ordered_keys],
            key=lambda key: original_order[key],
        )
        ordered_keys.extend(remaining)

    return [key_to_table[key] for key in ordered_keys]


def _reflect_table(sync_conn, schema: str, name: str) -> Table:
    metadata = MetaData()
    return Table(name, metadata, schema=schema, autoload_with=sync_conn)


def _row_from_csv(row: dict[str, str], table: Table) -> dict[str, Any]:
    converted: dict[str, Any] = {}
    for column_name, raw in row.items():
        column = table.columns.get(column_name)
        if column is None:
            continue
        converted[column_name] = parse_cell(raw, column.type)
    return converted


async def _upsert_table(
    conn: AsyncConnection,
    table_info: ArtifactTable,
    chunk_size: int,
) -> tuple[int, int]:
    table = await conn.run_sync(_reflect_table, table_info.schema, table_info.name)
    pk_columns = [column.name for column in table.primary_key.columns]
    if not pk_columns:
        raise ValueError(
            f"{table_info.key} has no primary key; cannot perform deterministic upsert"
        )

    affected_rows = 0
    processed_rows = 0
    batch: list[dict[str, Any]] = []

    with table_info.file_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for raw_row in reader:
            batch.append(_row_from_csv(raw_row, table))
            if len(batch) >= chunk_size:
                affected_rows += await _execute_upsert_batch(conn, table, pk_columns, batch)
                processed_rows += len(batch)
                batch.clear()

    if batch:
        affected_rows += await _execute_upsert_batch(conn, table, pk_columns, batch)
        processed_rows += len(batch)

    return processed_rows, affected_rows


async def _execute_upsert_batch(
    conn: AsyncConnection,
    table: Table,
    pk_columns: list[str],
    batch: list[dict[str, Any]],
) -> int:
    stmt = insert(table).values(batch)
    updatable_columns = [column.name for column in table.columns if column.name not in pk_columns]
    if updatable_columns:
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.columns[name] for name in pk_columns],
            set_={name: getattr(stmt.excluded, name) for name in updatable_columns},
        )
    else:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[table.columns[name] for name in pk_columns],
        )

    result = await conn.execute(stmt)
    return result.rowcount or 0


async def _run_upsert(
    artifact_dir: Path,
    database_url: str,
    chunk_size: int,
    truncate: bool,
) -> int:
    manifest = _load_manifest(artifact_dir)
    tables = _load_artifact_tables(artifact_dir, manifest)

    engine = create_async_engine(_normalize_database_url(database_url), echo=False)
    try:
        async with engine.begin() as conn:
            if truncate:
                await _truncate_and_cleanup(conn, tables)

            fk_edges = await _load_fk_edges(conn)
            ordered_tables = _sort_tables_by_fk(tables, fk_edges)

            summary = defaultdict(int)
            for table_info in ordered_tables:
                processed, affected = await _upsert_table(conn, table_info, chunk_size)
                summary["tables"] += 1
                summary["rows_processed"] += processed
                summary["rows_affected"] += affected
                print(f"upserted {table_info.key}: processed={processed}, affected={affected}")

        print(
            "summary: "
            f"tables={summary['tables']}, "
            f"rows_processed={summary['rows_processed']}, "
            f"rows_affected={summary['rows_affected']}"
        )
        return 0
    finally:
        await engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upsert seed artifact into a running DB")
    parser.add_argument(
        "--artifact-dir",
        help="Path to a partial dump artifact directory (contains manifest.json)",
    )
    parser.add_argument(
        "--partial-dump-root",
        default=str(DEFAULT_PARTIAL_DUMP_ROOT),
        help="Root directory used to auto-pick latest artifact when --artifact-dir is omitted",
    )
    parser.add_argument(
        "--database-url",
        help="Target database URL (default: env DATABASE_URL)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1000,
        help="Rows per upsert batch",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help=(
            "Reset managed schemas before upsert: drop obsolete tables "
            "(except public.alembic_version) and truncate imported tables"
        ),
    )
    return parser


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    csv.field_size_limit(min(sys.maxsize, 1024 * 1024 * 128))

    parser = _build_parser()
    args = parser.parse_args()

    artifact_dir = (
        Path(args.artifact_dir)
        if args.artifact_dir
        else _find_latest_artifact(Path(args.partial_dump_root))
    )

    database_url = args.database_url or os.getenv("DATABASE_URL", "")
    if not database_url.strip():
        print("error: DATABASE_URL is required (or pass --database-url)")
        return 2

    if args.chunk_size < 1:
        print("error: --chunk-size must be >= 1")
        return 2

    print(f"artifact: {artifact_dir}")
    print(f"target_db: {database_url.split('@')[-1]}")
    print(f"mode: {'truncate+upsert' if args.truncate else 'upsert'}")

    import asyncio

    try:
        return asyncio.run(
            _run_upsert(
                artifact_dir=artifact_dir,
                database_url=database_url,
                chunk_size=args.chunk_size,
                truncate=args.truncate,
            )
        )
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc!r}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
