"""
Pytest configuration and fixtures.
"""

import os
from inspect import signature
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import reset_source_rate_limit_state
from app.config import get_settings
from app.dependencies import get_db
from app.main import app
from app.models.base import Base

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://findb:findb@localhost:5435/findb_test",
)


def _requests_fixture_directly(request, fixture_name: str) -> bool:
    """Return whether the test function, rather than another fixture, requests a fixture."""
    return fixture_name in signature(request.node.obj).parameters


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def test_engine():
    """Create the test schema once for the whole test session."""
    await ensure_test_database()
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS raw"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_test_database(request):
    """Keep DB-backed tests isolated without rebuilding the schema for every test."""
    if not _requests_fixture_directly(request, "test_engine"):
        yield
        return

    engine = request.getfixturevalue("test_engine")
    yield

    table_names = []
    preparer = engine.dialect.identifier_preparer
    for table in Base.metadata.sorted_tables:
        table_name = preparer.quote(table.name)
        if table.schema:
            table_name = f"{preparer.quote_schema(table.schema)}.{table_name}"
        table_names.append(table_name)

    if table_names:
        async with engine.begin() as conn:
            await conn.execute(
                text(f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE")
            )


async def ensure_test_database():
    """Create the test database if it does not exist."""
    url = make_url(TEST_DATABASE_URL)
    admin_url = url.set(database="postgres")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db_name"),
                {"db_name": url.database},
            )
            exists = result.scalar_one_or_none() is not None
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_session(request, test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create an isolated test session.

    Most tests run inside an outer transaction so application-level commits remain
    visible to the test but are rolled back afterward. Tests that request the engine
    directly need committed data to be visible across connections, so they use a
    regular session and are cleaned by ``clean_test_database`` instead.
    """
    if _requests_fixture_directly(request, "test_engine"):
        session_factory = async_sessionmaker(
            test_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with session_factory() as session:
            yield session
        return

    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        session_factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            async with session_factory() as session:
                yield session
        finally:
            if transaction.is_active:
                await transaction.rollback()


@pytest_asyncio.fixture(scope="function")
async def client(test_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Create test HTTP client."""

    async def override_get_db():
        yield test_session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def source_api_key() -> str:
    """Get test source API key."""
    return "test-source-key"


@pytest.fixture(autouse=True)
def configure_source_api_key(source_api_key: str):
    """Ensure the Source API key is configured for tests."""
    settings = get_settings()
    original_key = settings.SOURCE_API_KEY
    original_debug = settings.DEBUG
    settings.SOURCE_API_KEY = source_api_key
    settings.DEBUG = True
    yield
    settings.SOURCE_API_KEY = original_key
    settings.DEBUG = original_debug


@pytest.fixture(autouse=True)
def reset_source_rate_limiter():
    reset_source_rate_limit_state()
    yield
    reset_source_rate_limit_state()


@pytest.fixture
def source_headers(source_api_key: str) -> dict:
    """Get headers for source API requests."""
    settings = get_settings()
    return {settings.API_KEY_HEADER: source_api_key}


@pytest.fixture
def admin_api_key() -> str:
    """Get test admin API key."""
    return "test-admin-key"


@pytest.fixture(autouse=True)
def configure_admin_api_key(admin_api_key: str):
    """Ensure the Admin API key is configured for tests."""
    settings = get_settings()
    original_key = settings.ADMIN_API_KEY
    settings.ADMIN_API_KEY = admin_api_key
    yield
    settings.ADMIN_API_KEY = original_key


@pytest.fixture
def admin_headers(admin_api_key: str) -> dict:
    """Get headers for admin API requests."""
    settings = get_settings()
    return {settings.API_KEY_HEADER: admin_api_key}
