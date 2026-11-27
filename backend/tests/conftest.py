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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import reset_source_rate_limit_state
from app.config import get_settings
from app.dependencies import get_db
from app.main import app
from app.models.base import Base
from tests.migration_database import (
    MigrationDatabaseFactory,
    set_active_migration_database_factory,
)
from tests.orm_database import orm_test_database

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://findb:findb@localhost:5435/findb_test",
)

# CPython swallows an "error" filter for this warning inside os.fork(), so a
# filterwarnings entry cannot enforce it; fail the session from the record.
FORK_WARNING_PREFIX = "This process (pid="
FORK_WARNING_MARKER = "is multi-threaded, use of fork() may lead to deadlocks"
_fork_warnings: list[str] = []


def pytest_warning_recorded(warning_message, when, nodeid, location):
    message = str(warning_message.message)
    if (
        issubclass(warning_message.category, DeprecationWarning)
        and message.startswith(FORK_WARNING_PREFIX)
        and FORK_WARNING_MARKER in message
    ):
        _fork_warnings.append(f"{nodeid or location}: {message}")


def pytest_sessionfinish(session, exitstatus):
    if _fork_warnings and exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    if _fork_warnings:
        terminalreporter.section("fork() from a multi-threaded test process")
        for line in _fork_warnings:
            terminalreporter.line(line)
        terminalreporter.line("Start child processes with subprocess/exec instead of fork().")


@pytest.fixture(autouse=True)
def pre_listing_schema(request, monkeypatch):
    """Migration tests seed older schemas through current services.

    Universe member listings postdate those schemas and no seed carries a
    listing date, so their reads see none instead of a missing table.
    """
    if "migration" in request.node.fspath.basename:
        from app.services import full_market

        async def no_listings(db, release_id):
            return {}

        monkeypatch.setattr(full_market, "_listing_dates", no_listings)


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def migration_database_factory():
    """Manage immutable migration templates for the entire pytest session."""

    factory = MigrationDatabaseFactory(TEST_DATABASE_URL)
    set_active_migration_database_factory(factory)
    try:
        yield factory
    finally:
        set_active_migration_database_factory(None)
        await factory.close()


def _requests_fixture_directly(request, fixture_name: str) -> bool:
    """Return whether the test function, rather than another fixture, requests a fixture."""
    return fixture_name in signature(request.node.obj).parameters


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def test_engine():
    """Create the ORM schema in a database owned by this pytest session only."""
    async with orm_test_database(TEST_DATABASE_URL) as engine:
        yield engine


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

    # DELETE (dependents first) instead of TRUNCATE: these tables hold a few
    # rows, and TRUNCATE of every table costs ~130ms per test, DELETE ~10ms.
    async with engine.begin() as conn:
        for table_name in reversed(table_names):
            await conn.execute(text(f"DELETE FROM {table_name}"))
        await conn.execute(
            text(
                "DO $$ DECLARE s record; BEGIN FOR s IN SELECT schemaname, sequencename "
                "FROM pg_sequences LOOP EXECUTE format('ALTER SEQUENCE %I.%I RESTART', "
                "s.schemaname, s.sequencename); END LOOP; END $$"
            )
        )


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
def owned_client(client, test_session):
    """Opt in to real owned-work setup for payload-focused tests."""
    from tests.owned_ingest import OwnedIngestClient

    return OwnedIngestClient(client, test_session)


@pytest.fixture(autouse=True)
def reset_source_rate_limiter():
    reset_source_rate_limit_state()
    yield
    reset_source_rate_limit_state()


@pytest_asyncio.fixture
async def source_headers(request, test_session: AsyncSession) -> AsyncGenerator[dict, None]:
    """Get a DB-backed Source client for the provider exercised by each suite."""
    from app.services.source_clients import create_source_client

    settings = get_settings()
    basename = request.node.fspath.basename
    provider = "finlab"
    allowed_datasets = ["tw_equity_eod"]
    if basename == "test_minute_normalize.py":
        from tests.market_minute_fixture import legacy_source_client as create_source_client

        provider = "shioaji"
        allowed_datasets = ["tw_equity_minute", "tw_etf_minute"]
    _, api_key = await create_source_client(
        test_session,
        name="test-source-client",
        owner="tests",
        source_name=provider,
        allowed_datasets=allowed_datasets,
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    yield {settings.API_KEY_HEADER: api_key}


@pytest_asyncio.fixture
async def admin_headers(test_session: AsyncSession) -> AsyncGenerator[dict, None]:
    """Get a DB-backed Owner machine credential for Admin endpoint tests."""
    from app.services.api_keys import create_api_key

    settings = get_settings()
    _, api_key = await create_api_key(
        test_session,
        owner="tests",
        tier="standard",
        scopes=["admin"],
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=1000,
        kind="admin",
        name="test-admin-machine",
        role="owner",
        commit=False,
    )
    yield {settings.API_KEY_HEADER: api_key}


@pytest.fixture(autouse=True)
def isolate_full_market_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "local")


# The members each provider's historical suites deliver when a test publishes no release.
HISTORY_RELEASE_MEMBERS = {
    "twelve_data": ("AAPL", "MSFT", "NVDA"),
    "finlab": ("2330", "2317"),
}


@pytest.fixture
def full_release_cohort(monkeypatch):
    """Twelve Data/FinLab historical requests freeze a real published Full release.

    A suite that did not publish one gets these members published as of the request's
    first date, so every request binds, verifies and completes its cohort like production.
    """
    import hashlib
    from decimal import Decimal

    from sqlalchemy import select

    from app.models.registry import UniverseMember, UniverseRelease
    from app.services import historical_backfill
    from app.utils import utc_now

    bind = historical_backfill._bind_full_releases

    async def bind_published(db, *, provider, dataset_key, days):
        if not days:
            return await bind(db, provider=provider, dataset_key=dataset_key, days=days)
        first = min(days)
        published = await db.scalar(
            select(UniverseRelease.release_id).where(
                UniverseRelease.provider == provider,
                UniverseRelease.dataset_key == dataset_key,
                UniverseRelease.status == "published",
                UniverseRelease.effective_date <= first,
            )
        )
        if published is None and provider in HISTORY_RELEASE_MEMBERS:
            us = provider == "twelve_data"
            symbols = HISTORY_RELEASE_MEMBERS[provider]
            digest = hashlib.sha256(",".join(symbols).encode()).hexdigest()
            release = UniverseRelease(
                dataset_key=dataset_key,
                provider=provider,
                effective_date=first,
                observed_at=utc_now(),
                source_timezone="America/New_York" if us else "Asia/Taipei",
                evidence=[{"url": "https://example.test/universe", "sha256": "a" * 64}],
                member_sha256=digest,
                semantic_member_sha256=digest,
                member_count=len(symbols),
                change_count=0,
                change_ratio=Decimal(0),
                status="published",
                published_at=utc_now(),
                published_by="test-owner",
            )
            db.add(release)
            await db.flush()
            db.add_all(
                UniverseMember(
                    release_id=release.release_id,
                    member_key=symbol,
                    symbol=symbol,
                    provider_symbol=symbol,
                    exchange="NASDAQ" if us else "TWSE",
                    currency="USD" if us else "TWD",
                    asset_class="etf" if dataset_key == "tw_etf_eod" else "equity",
                    mapping_status="mapped",
                )
                for symbol in symbols
            )
            await db.flush()
        return await bind(db, provider=provider, dataset_key=dataset_key, days=days)

    monkeypatch.setattr(historical_backfill, "_bind_full_releases", bind_published)
