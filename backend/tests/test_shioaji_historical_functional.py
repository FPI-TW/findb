"""Production data/FIFO behavior uses logical time, independent of time boundaries."""

import asyncio
import json
from collections import Counter

import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWork
from tests.provider_work_runtime_support import RuntimeHarness
from tests.test_provider_work_adapters import _assert_historical_completed


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_call", [1, 50], ids=["first-member", "last-member"])
async def test_production_shioaji_acquisition_retry_completes_all_original_members(
    client, test_session, test_engine, monkeypatch, tmp_path, caplog, failed_call
):
    from findb_fetcher.providers.shioaji import IsolatedShioajiGateway, ShioajiSdkError

    h = await RuntimeHarness(
        client,
        test_session,
        test_engine,
        monkeypatch,
        tmp_path,
        "shioaji",
        "tw_equity_minute",
        environment="production",
    ).initialize()
    h.execution_timeout = 90
    original = IsolatedShioajiGateway.fetch_kbars
    calls = 0
    secret = "provider-private-detail-must-not-enter-diagnostics"

    def fail_once(gateway, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failed_call:
            raise ShioajiSdkError(secret, code="ACQUISITION")
        return original(gateway, *args, **kwargs)

    monkeypatch.setattr(IsolatedShioajiGateway, "fetch_kbars", fail_once)
    try:
        assert await h.run()
        h.db.expire_all()
        work = await h.db.scalar(
            select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
        )
        assert work.state == "queued", h.shioaji_diagnostics
        frozen = work.manifest
        initial_calls = list(h.native_calls())
        first = h.shioaji_diagnostics[0]
        assert first["results"][-1]["code"] == "ACQUISITION_FAILED"
        assert first["results"][-1]["retryable"] is True
        assert len(h.runs) == failed_call - 1

        # No real minute needs to elapse: the same 61s reaches account, Source
        # lease/fence, SDK child and SQLite rolling limiter.
        assert h.shioaji_clock is not None
        h.shioaji_clock[0] += 61
        assert await h.run()
        await _assert_historical_completed(h)
        assert work.manifest == frozen
        assert len(h.runs) == 50
        assert calls == 51
        native = [json.loads(line) for line in h.native_calls()]
        symbols = Counter(call[1] for call in native if call[0] == "kbars")
        assert symbols == Counter(row["symbol"] for row in frozen["sequences"])
        assert h.native_calls()[: len(initial_calls)] == initial_calls
        final = h.shioaji_diagnostics[-1]
        assert len(final["results"]) == 50
        assert {result["code"] for result in final["results"]} == {"completed"}
        assert sorted(row["attempts"] for row in final["checkpoint"]) == [1] * 49 + [2]
        saved = [json.loads(line) for line in h.shioaji_diagnostic_path.read_text().splitlines()]
        assert saved == h.shioaji_diagnostics
        assert secret not in caplog.text + h.shioaji_diagnostic_path.read_text()
        assert h.config.source_client_key not in h.shioaji_diagnostic_path.read_text()
        assert "SHIOAJI_SEQUENCE_DIAGNOSTICS" in caplog.text
    finally:
        h.allow_confirm.set()
        h.allow_terminal.set()
        h.release_process_lock()


@pytest.mark.asyncio
async def test_shioaji_heartbeat_can_overlap_functional_delivery(
    client, test_session, test_engine, monkeypatch, tmp_path
):
    h = await RuntimeHarness(
        client,
        test_session,
        test_engine,
        monkeypatch,
        tmp_path,
        "shioaji",
        "tw_etf_minute",
    ).initialize()
    entered, release, contended = asyncio.Event(), asyncio.Event(), asyncio.Event()
    owners = []
    handle, request = h.handle, h.client.request

    class ObservedLock(asyncio.Lock):
        async def __aenter__(self):
            if self.locked():
                contended.set()
            return await super().__aenter__()

    h.source_session_lock = ObservedLock()

    def observe_owner(incoming):
        if incoming.url.path == "/api/v1/source/ingest" and not owners:
            owners.append(h.queue.active())
        return handle(incoming)

    async def paused_request(method, url, **kwargs):
        if str(url).endswith("/api/v1/source/ingest") and not entered.is_set():
            entered.set()
            await release.wait()
        return await request(method, url, **kwargs)

    h.handle = observe_owner
    monkeypatch.setattr(h.client, "request", paused_request)
    running = asyncio.create_task(h.run())
    beating = None
    try:
        await asyncio.wait_for(entered.wait(), 15)
        assert owners and owners[0] is not None
        beating = asyncio.create_task(asyncio.to_thread(owners[0].request, "heartbeat"))
        # Force contention directly; never wait for the wall-clock heartbeat
        # interval or depend on how fast CI can process a 50-member batch.
        await asyncio.wait_for(contended.wait(), 15)
        release.set()
        await beating
        assert await running
        await _assert_historical_completed(h)
        assert not owners[0].failed.is_set()
        assert not h.source_errors
        assert any(path.endswith("/heartbeat") and code == 200 for path, code, _ in h.source_events)
    finally:
        release.set()
        h.allow_confirm.set()
        h.allow_terminal.set()
        await asyncio.gather(running, *([beating] if beating else []), return_exceptions=True)
        h.release_process_lock()
