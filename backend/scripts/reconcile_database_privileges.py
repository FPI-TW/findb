"""Reconcile least-privilege grants for the FinDB application database role.

The migration connection owns schema objects while the long-lived application
connection must remain unable to perform DDL.  Production bootstrap therefore
runs this script after Alembic and before any application service starts.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

MANAGED_SCHEMAS = ("public", "raw")
_ROLE_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


class DatabasePrivilegeError(RuntimeError):
    """Raised when database identities or grants are unsafe."""


@dataclass(frozen=True)
class DatabaseBinding:
    """Validated migration and application identities for one database."""

    migration_url: URL
    application_url: URL
    migration_role: str
    application_role: str


def _endpoint(url: URL) -> tuple[str, int, str]:
    port = 5432 if url.port is None else url.port
    return ((url.host or "").casefold(), port, url.database or "")


def validate_database_binding(
    migration_database_url: str,
    application_database_url: str,
) -> DatabaseBinding:
    """Require distinct, simple roles bound to the same PostgreSQL database."""
    migration_url = make_url(migration_database_url)
    application_url = make_url(application_database_url)
    migration_role = migration_url.username or ""
    application_role = application_url.username or ""

    if migration_url.get_backend_name() != "postgresql" or (
        application_url.get_backend_name() != "postgresql"
    ):
        raise DatabasePrivilegeError("both database URLs must use PostgreSQL")
    if not all(_endpoint(migration_url)) or _endpoint(migration_url) != _endpoint(application_url):
        raise DatabasePrivilegeError(
            "database URLs must target the same explicit host and database"
        )
    if not _ROLE_PATTERN.fullmatch(migration_role) or not _ROLE_PATTERN.fullmatch(application_role):
        raise DatabasePrivilegeError("database roles must use simple lowercase identifiers")
    if migration_role == application_role:
        raise DatabasePrivilegeError("migration and application database roles must be distinct")

    return DatabaseBinding(
        migration_url=migration_url,
        application_url=application_url,
        migration_role=migration_role,
        application_role=application_role,
    )


async def _validate_roles(connection: AsyncConnection, binding: DatabaseBinding) -> None:
    current_role = str(await connection.scalar(text("SELECT current_user")) or "")
    if current_role != binding.migration_role:
        raise DatabasePrivilegeError("migration URL identity does not match current database role")

    role = (
        (
            await connection.execute(
                text(
                    """
                SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb
                FROM pg_roles
                WHERE rolname = :role
                """
                ),
                {"role": binding.application_role},
            )
        )
        .mappings()
        .one_or_none()
    )
    if role is None:
        raise DatabasePrivilegeError("application database role does not exist")
    if not role["rolcanlogin"] or any(
        role[flag] for flag in ("rolsuper", "rolcreaterole", "rolcreatedb")
    ):
        raise DatabasePrivilegeError("application database role violates least-privilege policy")


async def _verify_grants(connection: AsyncConnection, application_role: str) -> None:
    for schema in MANAGED_SCHEMAS:
        usage = bool(
            await connection.scalar(
                text("SELECT has_schema_privilege(:role, :schema, 'USAGE')"),
                {"role": application_role, "schema": schema},
            )
        )
        create = bool(
            await connection.scalar(
                text("SELECT has_schema_privilege(:role, :schema, 'CREATE')"),
                {"role": application_role, "schema": schema},
            )
        )
        if not usage or create:
            raise DatabasePrivilegeError(
                f"application schema privileges are unsafe for managed schema {schema}"
            )

        missing_table_grants = int(
            await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM pg_class AS relation
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = :schema
                      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
                      AND NOT (
                        namespace.nspname = 'public'
                        AND relation.relname = 'alembic_version'
                      )
                      AND NOT (
                        has_table_privilege(:role, relation.oid, 'SELECT')
                        AND has_table_privilege(:role, relation.oid, 'INSERT')
                        AND has_table_privilege(:role, relation.oid, 'UPDATE')
                        AND has_table_privilege(:role, relation.oid, 'DELETE')
                      )
                    """
                ),
                {"role": application_role, "schema": schema},
            )
            or 0
        )
        missing_sequence_grants = int(
            await connection.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM pg_class AS relation
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = :schema
                      AND relation.relkind = 'S'
                      AND NOT (
                        has_sequence_privilege(:role, relation.oid, 'USAGE')
                        AND has_sequence_privilege(:role, relation.oid, 'SELECT')
                        AND has_sequence_privilege(:role, relation.oid, 'UPDATE')
                      )
                    """
                ),
                {"role": application_role, "schema": schema},
            )
            or 0
        )
        if missing_table_grants or missing_sequence_grants:
            raise DatabasePrivilegeError(
                f"application object grants are incomplete for managed schema {schema}"
            )

    revision_privileges = (
        (
            await connection.execute(
                text(
                    """
                    SELECT
                      to_regclass('public.alembic_version') IS NOT NULL AS present,
                      has_table_privilege(:role, 'public.alembic_version', 'SELECT') AS can_select,
                      has_table_privilege(:role, 'public.alembic_version', 'INSERT') AS can_insert,
                      has_table_privilege(:role, 'public.alembic_version', 'UPDATE') AS can_update,
                      has_table_privilege(:role, 'public.alembic_version', 'DELETE') AS can_delete
                    """
                ),
                {"role": application_role},
            )
        )
        .mappings()
        .one()
    )
    if (
        not revision_privileges["present"]
        or not revision_privileges["can_select"]
        or any(revision_privileges[name] for name in ("can_insert", "can_update", "can_delete"))
    ):
        raise DatabasePrivilegeError(
            "application role must have read-only access to Alembic revision state"
        )


async def reconcile_database_privileges(
    migration_database_url: str,
    application_database_url: str,
) -> DatabaseBinding:
    """Grant runtime DML privileges while preserving the migration-only DDL boundary."""
    binding = validate_database_binding(migration_database_url, application_database_url)
    engine = create_async_engine(binding.migration_url, pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as connection:
            await _validate_roles(connection, binding)
            quote = engine.sync_engine.dialect.identifier_preparer.quote_identifier
            application_role = quote(binding.application_role)
            migration_role = quote(binding.migration_role)
            for schema in MANAGED_SCHEMAS:
                quoted_schema = quote(schema)
                await connection.exec_driver_sql(
                    f"GRANT USAGE ON SCHEMA {quoted_schema} TO {application_role}"
                )
                await connection.exec_driver_sql(
                    "GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA {quoted_schema} TO {application_role}"
                )
                await connection.exec_driver_sql(
                    "GRANT USAGE, SELECT, UPDATE "
                    f"ON ALL SEQUENCES IN SCHEMA {quoted_schema} TO {application_role}"
                )
                await connection.exec_driver_sql(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_role} "
                    f"IN SCHEMA {quoted_schema} GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON TABLES TO {application_role}"
                )
                await connection.exec_driver_sql(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {migration_role} "
                    f"IN SCHEMA {quoted_schema} GRANT USAGE, SELECT, UPDATE "
                    f"ON SEQUENCES TO {application_role}"
                )
            await connection.exec_driver_sql(
                "REVOKE INSERT, UPDATE, DELETE ON TABLE public.alembic_version "
                f"FROM {application_role}"
            )
            await connection.exec_driver_sql(
                f"GRANT SELECT ON TABLE public.alembic_version TO {application_role}"
            )
            await _verify_grants(connection, binding.application_role)
    finally:
        await engine.dispose()
    return binding


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migration-database-url",
        default=os.getenv("DATABASE_URL"),
        help="migration connection URL; defaults to DATABASE_URL",
    )
    parser.add_argument(
        "--application-database-url",
        default=os.getenv("APPLICATION_DATABASE_URL"),
        help="application connection URL; defaults to APPLICATION_DATABASE_URL",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.migration_database_url or not args.application_database_url:
        print("database_privileges=failed reason=database_url_missing", file=sys.stderr)
        return 1
    try:
        binding = asyncio.run(
            reconcile_database_privileges(
                args.migration_database_url,
                args.application_database_url,
            )
        )
    except (DatabasePrivilegeError, SQLAlchemyError, OSError):
        print("database_privileges=failed reason=reconciliation_failed", file=sys.stderr)
        return 1
    print(
        "database_privileges=ready "
        f"migration_role={binding.migration_role} application_role={binding.application_role}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
