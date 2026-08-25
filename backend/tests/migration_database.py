"""Session-scoped PostgreSQL template databases for migration tests.

Migration tests intentionally exercise historical revisions, but rebuilding the
same Alembic chain for every test makes the suite unnecessarily expensive.  This
module builds immutable databases once per pytest session and clones the required
revision for each test with PostgreSQL's ``CREATE DATABASE ... TEMPLATE``.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MAX_DATABASE_IDENTIFIER_LENGTH = 63

# Keep this list in Alembic order.  Every historical database used by the
# migration regression tests starts at one of these revisions; later revisions
# are applied only inside the test's cloned database.
MIGRATION_TEMPLATE_REVISIONS = (
    "f7a8b9c0d1e2",
    "f8a9b0c1d2e3",
    "a9b0c1d2e3f4",
    "c1d2e3f4a5b6",
    "e3f4a5b6c7d8",
    "f4a5b6c7d8e9",
    "d4e5f6a7b8c9",
    "e5f6a7b8c9d0",
    "f2a3b4c5d6e7",
    "a3b4c5d6e7f8",
    "b4c5d6e7f8a9",
    "c5d6e7f8a9b0",
)

_REVISION_PATTERN = re.compile(r"^[0-9a-f]{12}$")
_DATABASE_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_ACTIVE_FACTORY: MigrationDatabaseFactory | None = None


def _quote_identifier(identifier: str) -> str:
    """Quote a PostgreSQL identifier without allowing SQL injection."""

    if not _DATABASE_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"Unsafe migration database identifier: {identifier!r}")
    return '"' + identifier.replace('"', '""') + '"'


def _safe_identifier(*parts: str) -> str:
    """Return a lowercase PostgreSQL identifier within the 63-byte limit."""

    value = re.sub(r"[^a-z0-9_]+", "_", "_".join(parts).lower()).strip("_")
    value = value or "findb_migration"
    return value[:MAX_DATABASE_IDENTIFIER_LENGTH].rstrip("_") or "findb_migration"


class MigrationDatabaseFactory:
    """Build and clone immutable historical migration databases for one session."""

    def __init__(self, base_database_url: str) -> None:
        self._base_url: URL = make_url(base_database_url)
        worker_token = _safe_identifier(os.getenv("PYTEST_XDIST_WORKER", "worker"))[:8]
        self._session_token = f"{worker_token}_{uuid4().hex[:12]}"
        self._admin_engine: AsyncEngine | None = None
        self._templates: dict[str, str] = {}
        self._created_databases: list[str] = []
        self._lock = asyncio.Lock()

    async def _admin(self) -> AsyncEngine:
        if self._admin_engine is None:
            self._admin_engine = create_async_engine(
                self._base_url.set(database="postgres"),
                isolation_level="AUTOCOMMIT",
            )
        return self._admin_engine

    def _template_name(self, revision: str) -> str:
        return _safe_identifier("findb_migration_template", self._session_token, revision)

    def _clone_name(self, prefix: str) -> str:
        clone_token = uuid4().hex[:12]
        safe_prefix = _safe_identifier(prefix)
        fixed = _safe_identifier("findb_migration_clone", self._session_token)
        available = MAX_DATABASE_IDENTIFIER_LENGTH - len(fixed) - len(clone_token) - 2
        return _safe_identifier(fixed, safe_prefix[: max(1, available)], clone_token)

    def _database_url(self, database_name: str) -> str:
        return self._base_url.set(database=database_name).render_as_string(hide_password=False)

    async def _create_database(self, database_name: str, template_name: str | None = None) -> None:
        statement = f"CREATE DATABASE {_quote_identifier(database_name)}"
        if template_name is not None:
            statement += f" TEMPLATE {_quote_identifier(template_name)}"

        admin_engine = await self._admin()
        created = False
        try:
            async with admin_engine.connect() as connection:
                await connection.execute(text(statement))
            created = True
            self._created_databases.append(database_name)
            if template_name is not None:
                # PostgreSQL generally copies this flag from the source.  Make
                # clones explicitly connectable even when templates are frozen.
                async with admin_engine.connect() as connection:
                    await connection.execute(
                        text(
                            f"ALTER DATABASE {_quote_identifier(database_name)} "
                            "WITH ALLOW_CONNECTIONS true"
                        )
                    )
        except Exception:
            if created:
                try:
                    await self._drop_database(database_name)
                except Exception:
                    # Preserve the CREATE/ALTER failure and let session
                    # teardown retry the tracked database drop.
                    pass
            raise

    async def _drop_database(self, database_name: str) -> None:
        admin_engine = await self._admin()
        dropped = False
        try:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    text(f"DROP DATABASE IF EXISTS {_quote_identifier(database_name)} WITH (FORCE)")
                )
            dropped = True
        finally:
            if dropped and database_name in self._created_databases:
                self._created_databases.remove(database_name)

    async def _run_alembic(self, database_url: str, revision: str) -> None:
        environment = os.environ.copy()
        environment["DATABASE_URL"] = database_url
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", revision],
            cwd=BACKEND_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Alembic upgrade {revision} failed:\n{result.stdout}\n{result.stderr}"
            )

    async def _verify_stamp(self, database_url: str, revision: str) -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                stamp = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        finally:
            await engine.dispose()
        if stamp != revision:
            raise RuntimeError(
                f"Migration template {revision} has unexpected Alembic stamp {stamp!r}"
            )

    async def _freeze_template(self, database_name: str) -> None:
        admin_engine = await self._admin()
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(
                    f"ALTER DATABASE {_quote_identifier(database_name)} "
                    "WITH ALLOW_CONNECTIONS false"
                )
            )
            allow_connections = await connection.scalar(
                text("SELECT datallowconn FROM pg_database WHERE datname = :database_name"),
                {"database_name": database_name},
            )
        if allow_connections is not False:
            raise RuntimeError(f"Migration template {database_name} was not frozen")

    async def _ensure_template(self, revision: str) -> str:
        if (
            not _REVISION_PATTERN.fullmatch(revision)
            or revision not in MIGRATION_TEMPLATE_REVISIONS
        ):
            raise ValueError(f"Unsupported migration template revision: {revision}")

        async with self._lock:
            if revision in self._templates:
                return self._templates[revision]

            target_index = MIGRATION_TEMPLATE_REVISIONS.index(revision)
            created_this_call: list[tuple[str, str]] = []
            try:
                for index, current_revision in enumerate(
                    MIGRATION_TEMPLATE_REVISIONS[: target_index + 1]
                ):
                    if current_revision in self._templates:
                        continue
                    source = (
                        "template0"
                        if index == 0
                        else self._templates[MIGRATION_TEMPLATE_REVISIONS[index - 1]]
                    )
                    database_name = self._template_name(current_revision)
                    await self._create_database(database_name, source)
                    created_this_call.append((current_revision, database_name))
                    database_url = self._database_url(database_name)
                    await self._run_alembic(database_url, current_revision)
                    await self._verify_stamp(database_url, current_revision)
                    await self._freeze_template(database_name)
                    self._templates[current_revision] = database_name
                return self._templates[revision]
            except Exception:
                for current_revision, database_name in reversed(created_this_call):
                    self._templates.pop(current_revision, None)
                    try:
                        await self._drop_database(database_name)
                    except Exception:
                        # Preserve the original migration error while still
                        # attempting every best-effort cleanup target.
                        pass
                raise

    @asynccontextmanager
    async def clone(
        self,
        revision: str,
        prefix: str,
    ) -> AsyncIterator[tuple[str, AsyncEngine]]:
        """Yield a disposable clone at ``revision`` and always drop it afterward."""

        template_name = await self._ensure_template(revision)
        database_name = self._clone_name(prefix)
        database_url = self._database_url(database_name)
        target_engine: AsyncEngine | None = None
        database_created = False
        try:
            await self._create_database(database_name, template_name)
            database_created = True
            target_engine = create_async_engine(database_url)
            yield database_url, target_engine
        finally:
            if target_engine is not None:
                await target_engine.dispose()
            if database_created:
                await self._drop_database(database_name)

    async def close(self) -> None:
        """Drop all session databases in reverse creation order."""

        cleanup_error: Exception | None = None
        for database_name in reversed(self._created_databases.copy()):
            try:
                await self._drop_database(database_name)
            except Exception as exc:  # pragma: no cover - defensive teardown path
                cleanup_error = cleanup_error or exc
        self._templates.clear()
        if self._admin_engine is not None:
            await self._admin_engine.dispose()
            self._admin_engine = None
        if cleanup_error is not None:
            raise cleanup_error


def set_active_migration_database_factory(
    factory: MigrationDatabaseFactory | None,
) -> None:
    """Set the factory used by migration tests in the current pytest session."""

    global _ACTIVE_FACTORY
    _ACTIVE_FACTORY = factory


def get_active_migration_database_factory() -> MigrationDatabaseFactory:
    """Return the session factory installed by the pytest fixture."""

    if _ACTIVE_FACTORY is None:
        raise RuntimeError("migration_database_factory fixture is not active")
    return _ACTIVE_FACTORY
