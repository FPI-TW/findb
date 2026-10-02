"""Disposable PostgreSQL databases owned by ordinary ORM test sessions."""

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from secrets import token_hex

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.models.base import Base


def _database_name() -> str:
    # The fixed ASCII prefix plus bounded worker token leaves the complete random
    # suffix intact within PostgreSQL's 63-byte identifier limit.
    worker = re.sub(r"[^a-z0-9_]", "_", os.getenv("PYTEST_XDIST_WORKER", "worker").lower())[:8]
    return f"findb_orm_{worker}_{token_hex(16)}"


async def _initialize_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("CREATE SCHEMA raw"))
        await connection.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def orm_test_database(base_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Create, initialize and drop only this context's fresh test database.

    The configured database supplies server/credential options only. Never connect
    to it or change its schema. A failed CREATE (including a name collision) does
    not grant ownership and must never trigger DROP of the existing database.
    """
    base_url = make_url(base_database_url)
    database_name = _database_name()
    admin_engine = create_async_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    identifier = admin_engine.dialect.identifier_preparer.quote_identifier(database_name)
    engine: AsyncEngine | None = None
    created = False
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f"CREATE DATABASE {identifier} TEMPLATE template0"))
            created = True
        engine = create_async_engine(base_url.set(database=database_name), echo=False)
        await _initialize_schema(engine)
        yield engine
    finally:
        try:
            try:
                if engine is not None:
                    await engine.dispose()
            finally:
                if created:
                    async with admin_engine.connect() as connection:
                        await connection.execute(text(f"DROP DATABASE {identifier} WITH (FORCE)"))
        finally:
            await admin_engine.dispose()
