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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

import app.models  # noqa: F401  # register every ORM table on Base.metadata
from app.models import Base
from app.utils import uuid7
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
    async with get_active_migration_database_factory().clone("1587769858c0", "schema_drift") as (
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
async def test_orm_check_constraints_match_upgraded_schema() -> None:
    _, check_diffs = await _upgraded_schema_diffs()

    assert check_diffs == []


@pytest.mark.asyncio
async def test_orm_server_defaults_match_upgraded_schema() -> None:
    metadata_diffs, _ = await _upgraded_schema_diffs()

    assert [(diff[2], diff[3]) for diff in metadata_diffs if diff[0] == "modify_default"] == []


async def _insert_calendar_market(db: AsyncConnection, market: str) -> None:
    await db.execute(
        text(
            "INSERT INTO calendar_market "
            "(market, display_name, timezone, weekend_days, created_at, updated_at) "
            "VALUES (:market, 'Schema parity', 'Asia/Taipei', '[5,6]', now(), now())"
        ),
        {"market": market},
    )


async def _assert_raw_defaults(db: AsyncConnection) -> None:
    """Bypass ORM Python defaults to exercise actual database INSERT behavior."""
    user = (
        await db.execute(
            text(
                "INSERT INTO admin_user "
                "(user_id, username, display_name, password_hash, role, created_at, updated_at) "
                "VALUES (:id, 'schema-parity', 'Parity', 'test-hash', 'viewer', now(), now()) "
                "RETURNING is_active, must_change_password"
            ),
            {"id": uuid7()},
        )
    ).one()
    assert user == (True, False)
    kind = await db.scalar(
        text(
            "INSERT INTO api_key (key_id, key_hash, owner, tier, scopes, "
            "rate_limit_requests, rate_limit_window, page_size_limit, usage_count, created_at) "
            "VALUES (:id, 'schema-parity', 'parity', 'standard', '[]', 100, 60, 1000, 0, now()) "
            "RETURNING kind"
        ),
        {"id": uuid7()},
    )
    assert kind == "serve"
    limits = (
        await db.execute(
            text(
                "INSERT INTO source_client "
                "(client_id, name, source_name, key_hash, created_at, updated_at) "
                "VALUES (:id, 'schema-parity', 'shioaji', 'schema-parity', now(), now()) "
                "RETURNING rate_limit_requests, rate_limit_window, usage_count"
            ),
            {"id": uuid7()},
        )
    ).one()
    assert limits == (100, 60, 0)
    count = await db.scalar(
        text(
            "INSERT INTO credential_usage_rollup "
            "(usage_id, credential_kind, credential_id, updated_at) "
            "VALUES (:id, 'source', :credential, now()) RETURNING request_count"
        ),
        {"id": uuid7(), "credential": uuid7()},
    )
    assert count == 0
    await db.execute(
        text(
            "INSERT INTO dataset_registry "
            "(dataset_key, name, asset_class, market, frequency, is_active, config, "
            "created_at, updated_at) VALUES "
            "('schema_parity', 'Parity', 'equity', 'TW', 'daily', true, '{}', now(), now())"
        )
    )
    await _insert_calendar_market(db, "X_NEW")
    assert await db.scalar(text("SELECT active FROM calendar_market WHERE market = 'X_NEW'"))
    # A timestamp-without-timezone default cast to timestamptz would shift by 8h.
    original_timezone = await db.scalar(text("SHOW TIME ZONE"))
    await db.execute(text("SET LOCAL TIME ZONE 'Asia/Taipei'"))
    calendar = (
        await db.execute(
            text(
                "INSERT INTO trading_calendar (id, market, trade_date, is_open) "
                "VALUES (:id, 'X_NEW', '2026-10-29', true) "
                "RETURNING day_status, source_kind, revision, created_at, updated_at, now()"
            ),
            {"id": uuid7()},
        )
    ).one()
    assert calendar[:3] == ("open", "observed_ingestion", 0)
    assert calendar.created_at == calendar.updated_at == calendar[5]
    assert calendar.created_at.tzinfo is not None
    await db.execute(
        text("SELECT set_config('TimeZone', :timezone, true)"), {"timezone": original_timezone}
    )


@pytest.mark.asyncio
async def test_orm_server_defaults_apply_to_raw_sql(test_engine: AsyncEngine) -> None:
    async with test_engine.begin() as db:
        await _assert_raw_defaults(db)
        with pytest.raises(IntegrityError, match="calendar_market_code_valid"):
            async with db.begin_nested():
                await _insert_calendar_market(db, "tw")
