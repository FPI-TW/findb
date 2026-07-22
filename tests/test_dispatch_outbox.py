"""Dispatcher maintenance failure-isolation tests."""

import logging

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scripts import dispatch_outbox


@pytest.mark.asyncio
async def test_stale_attempt_reconciliation_failure_does_not_escape(
    test_engine,
    monkeypatch,
    caplog,
):
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def fail_after_query(session):
        await session.execute(text("SELECT 1"))
        raise RuntimeError("maintenance failed")

    monkeypatch.setattr(
        dispatch_outbox,
        "reconcile_stale_ingestion_attempts",
        fail_after_query,
    )

    with caplog.at_level(logging.ERROR):
        result = await dispatch_outbox.reconcile_stale_attempts_safely(session_factory)

    assert result == 0
    assert "Failed to reconcile stale ingestion attempts" in caplog.text
    async with session_factory() as session:
        assert await session.scalar(text("SELECT 1")) == 1
