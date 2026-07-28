"""Dispatcher maintenance failure-isolation tests."""

import asyncio
import logging
from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_delivery_monitor_failure_does_not_escape_and_rolls_back(
    test_engine, monkeypatch, caplog
):
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def fail_after_query(session, **kwargs):
        await session.execute(text("SELECT 1"))
        raise RuntimeError("monitor failed")

    monkeypatch.setattr(dispatch_outbox, "scan_missing_deliveries", fail_after_query)
    with caplog.at_level(logging.ERROR):
        await dispatch_outbox.scan_missing_deliveries_safely(session_factory)
    assert "Failed to scan missing dataset deliveries" in caplog.text
    async with session_factory() as session:
        assert await session.scalar(text("SELECT 1")) == 1


@pytest.mark.asyncio
async def test_delivery_monitor_timeout_rolls_back_and_is_isolated(
    test_engine, monkeypatch, caplog
):
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def block_after_query(session, **kwargs):
        await session.execute(text("SELECT 1"))
        await asyncio.Event().wait()

    monkeypatch.setattr(dispatch_outbox, "scan_missing_deliveries", block_after_query)
    monkeypatch.setattr(dispatch_outbox.settings, "DELIVERY_MONITOR_TIMEOUT_SECONDS", 0.01)
    with caplog.at_level(logging.ERROR):
        await dispatch_outbox.scan_missing_deliveries_safely(session_factory)
    assert "scan timed out" in caplog.text
    async with session_factory() as session:
        assert await session.scalar(text("SELECT 1")) == 1


@pytest.mark.asyncio
async def test_blocked_delivery_monitor_does_not_block_outbox_publish(monkeypatch) -> None:
    monitor_started = asyncio.Event()
    published = asyncio.Event()
    claimed = 0
    stale_reconciliations = 0

    class StopDispatcherError(RuntimeError):
        pass

    async def blocked_scan(session, **kwargs):
        monitor_started.set()
        await asyncio.Event().wait()

    async def claim(session):
        nonlocal claimed
        claimed += 1
        if claimed == 1:
            await asyncio.wait_for(monitor_started.wait(), timeout=1)
            return [SimpleNamespace(outbox_id="outbox-1")]
        raise StopDispatcherError

    async def repair_stale(*args, **kwargs):
        nonlocal stale_reconciliations
        stale_reconciliations += 1
        return 0

    async def no_reconciliation(*args, **kwargs):
        return 0

    async def mark_published(session, outbox_id):
        published.set()

    monkeypatch.setattr(dispatch_outbox, "declare_topology", lambda: None)
    monkeypatch.setattr(dispatch_outbox, "reconcile_stale_jobs", repair_stale)
    monkeypatch.setattr(dispatch_outbox, "reconcile_stale_attempts_safely", no_reconciliation)
    monkeypatch.setattr(dispatch_outbox, "scan_missing_deliveries", blocked_scan)
    monkeypatch.setattr(dispatch_outbox, "claim_outbox_batch", claim)
    monkeypatch.setattr(dispatch_outbox, "publish_outbox_event", lambda row: None)
    monkeypatch.setattr(dispatch_outbox, "mark_outbox_published", mark_published)
    monkeypatch.setattr(dispatch_outbox.settings, "DELIVERY_MONITOR_TIMEOUT_SECONDS", 60)

    with pytest.raises(StopDispatcherError):
        await asyncio.wait_for(dispatch_outbox.run_dispatcher(), timeout=2)
    assert published.is_set()
    assert stale_reconciliations >= 1
