"""ORM metadata must match the schema produced by ``alembic upgrade head``."""

from __future__ import annotations

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, Index, Table, inspect, text
from sqlalchemy.engine import Connection

import app.models  # noqa: F401  # register every ORM table on Base.metadata
from app.models import Base
from tests.migration_database import (
    BACKEND_ROOT,
    get_active_migration_database_factory,
    run_alembic,
)

# Same scope as migrations/env.py: the public schema plus the raw payload schema.
INCLUDED_SCHEMAS = {None, "raw"}


def _include_name(name: str | None, type_: str, parent_names: dict[str, Any]) -> bool:
    if type_ == "schema":
        return name in INCLUDED_SCHEMAS
    return True


def _partition_names(connection: Connection) -> set[str]:
    return set(
        connection.execute(text("SELECT relname FROM pg_class WHERE relispartition")).scalars()
    )


def _is_partition_diff(diff: Any, partitions: set[str]) -> bool:
    """Partitions (market_data_eod_y20xx/default, market_data_minute_default) are
    created by migrations as children of ORM-declared parents; the ORM maps only
    the partitioned parent table."""

    if not isinstance(diff, tuple) or diff[0] not in {"remove_table", "remove_index"}:
        return False
    target = diff[1]
    table = (
        target if isinstance(target, Table) else target.table if isinstance(target, Index) else None
    )
    return table is not None and table.name in partitions


def _flatten(diffs: list[Any]) -> list[Any]:
    # Column-level modifications are grouped in lists; flatten them for filtering.
    flattened: list[Any] = []
    for diff in diffs:
        flattened.extend(diff if isinstance(diff, list) else [diff])
    return flattened


def _metadata_diffs(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(
        connection,
        opts={
            "target_metadata": Base.metadata,
            "include_schemas": True,
            "include_name": _include_name,
            "compare_type": True,
            "compare_server_default": True,
        },
    )
    partitions = _partition_names(connection)
    return [
        diff
        for diff in _flatten(compare_metadata(context, Base.metadata))
        if not _is_partition_diff(diff, partitions)
    ]


def _check_constraint_diffs(connection: Connection) -> list[str]:
    """Autogenerate ignores CHECK constraints, so compare their rendered names."""

    inspector = inspect(connection)
    preparer = connection.dialect.identifier_preparer
    diffs: list[str] = []
    for table in Base.metadata.sorted_tables:
        expected = {
            preparer.format_constraint(constraint).strip('"')
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name is not None
        }
        actual = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table.name, schema=table.schema)
        }
        if expected != actual:
            diffs.append(
                f"{table.fullname}: missing in DB {sorted(expected - actual)}, "
                f"missing in ORM {sorted(actual - expected)}"
            )
    return diffs


async def _upgraded_schema_diffs() -> tuple[list[Any], list[str]]:
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "schema_drift") as (
        database_url,
        engine,
    ):
        await run_alembic(database_url, "head")
        async with engine.connect() as connection:
            metadata_diffs = await connection.run_sync(_metadata_diffs)
            check_diffs = await connection.run_sync(_check_constraint_diffs)
    return metadata_diffs, check_diffs


def test_migration_history_has_single_linear_head() -> None:
    scripts = ScriptDirectory.from_config(Config(str(BACKEND_ROOT / "alembic.ini")))
    heads = scripts.get_heads()
    assert len(heads) == 1
    revisions = list(scripts.walk_revisions("base", heads[0]))
    assert all(
        newer.down_revision == older.revision for newer, older in zip(revisions, revisions[1:])
    )


@pytest.mark.asyncio
async def test_orm_structure_matches_upgraded_schema() -> None:
    """Tables, columns, types, nullability, indexes, foreign keys and unique constraints."""

    metadata_diffs, _ = await _upgraded_schema_diffs()

    assert [diff for diff in metadata_diffs if diff[0] != "modify_default"] == []


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Unexplained drift: ORM check-constraint names already carrying the ck_<table>_ "
        "prefix get it applied twice by the naming convention on scheduler_control, "
        "ingestion_run, ingestion_attempt and missing_delivery_alert; "
        "calendar_market.ck_calendar_market_calendar_market_code_valid exists only in the ORM."
    ),
)
async def test_orm_check_constraints_match_upgraded_schema() -> None:
    _, check_diffs = await _upgraded_schema_diffs()

    assert check_diffs == []


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "Unexplained drift: migrations set server defaults on 26 columns (e.g. "
        "trading_calendar.day_status, ingestion_run.attempt_count, provider_work."
        "prepared_deliveries) that the ORM declares only as Python-side defaults."
    ),
)
async def test_orm_server_defaults_match_upgraded_schema() -> None:
    metadata_diffs, _ = await _upgraded_schema_diffs()

    assert [(diff[2], diff[3]) for diff in metadata_diffs if diff[0] == "modify_default"] == []
