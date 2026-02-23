"""
Pytest configuration and fixtures.
"""

import os
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.main import app
from app.models.base import Base
from app.dependencies import get_db
from app.config import get_settings
from app.api.deps import reset_source_rate_limit_state

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://findb:findb@localhost:5435/findb_test",
)


@pytest_asyncio.fixture(scope="function")
async def test_engine():
    """Create test database engine."""
    await ensure_test_database()
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS raw"))
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


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
async def test_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
    async_session = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session() as session:
        yield session


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
def configure_source_api_keys(source_api_key: str):
    """Ensure Source API keys are configured for tests."""
    settings = get_settings()
    original_keys = settings.SOURCE_API_KEYS
    original_debug = settings.DEBUG
    settings.SOURCE_API_KEYS = source_api_key
    settings.DEBUG = True
    yield
    settings.SOURCE_API_KEYS = original_keys
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
