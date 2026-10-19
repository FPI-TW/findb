"""Dispatched native 429s retain historical operation budget across queue restarts."""

import sqlite3
import time
from contextlib import closing
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import HistoricalBackfillRequest
from app.utils import utc_now
from tests.provider_work_clock import account_clock
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod")], indirect=True)
@pytest.mark.parametrize("fault", ["http429", "payload429"])
@pytest.mark.parametrize("recover", [False, True])
async def test_historical_native_rate_limit_preserves_budget_cooldown_and_scope_across_restart(
    native_runtime, test_engine, monkeypatch, fault, recover
):
    from findb_fetcher import historical_runtime
    from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig

    h = native_runtime
    offset = account_clock(monkeypatch)
    successful = historical_runtime.TwelveDataClient(None)
    handler = successful._client._transport.handle_request
    calls = []
    maximum = historical_runtime._schedule(h.provider, h.dataset).max_attempts

    def native(request):
        calls.append(request.url.params["symbol"])
        if recover and len(calls) >= maximum:
            return handler(request)
        return httpx.Response(
            429 if fault == "http429" else 200,
            json={"status": "error", "code": 429},
            headers={"Retry-After": "120"},
            request=request,
        )

    monkeypatch.setattr(
        historical_runtime,
        "TwelveDataClient",
        lambda config: TwelveDataClient(
            TwelveDataConfig("offline-twelve"),
            client=httpx.Client(transport=httpx.MockTransport(native)),
        ),
    )
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    work_id, scope = work.work_id, dict(work.scope)
    governor = h.path / "account/governor.sqlite3"
    for number in range(1, maximum + 1):
        assert await h.run()  # Every invocation builds a new runner/client/journal.
        h.db.expire_all()
        work = await h.db.get(ProviderWork, work_id)
        assert work.scope == scope
        journal = h.queue.WorkJournal(h.path / "account/provider-work.sqlite3", str(work_id))
        with journal.connection() as db:
            charged = db.execute(
                "SELECT sum(attempts) FROM work_budget WHERE work_key=?", (str(work_id),)
            ).fetchone()[0]
        expected_budget = number if number < maximum or not recover else maximum - 1
        assert charged == expected_budget
        if recover and number == maximum:
            break
        assert work.state == "queued" and not work.prepared_deliveries and not h.runs
        with closing(sqlite3.connect(governor)) as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM acquisition_intent WHERE actual_start IS NOT NULL"
                ).fetchone()[0]
                == number
            )
            assert (
                db.execute("SELECT max(next_at) FROM full_pacing").fetchone()[0]
                > time.time() + offset[0]
            )
        attempt = await h.db.scalar(
            select(ProviderWorkAttempt).where(
                ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
            )
        )
        assert attempt.not_before > utc_now()
        attempt.not_before = utc_now() - timedelta(seconds=1)
        await h.db.commit()
        offset[0] += 121
    if not recover:
        assert await h.run(), "budget exhaustion must settle manually without another dispatch"
        h.db.expire_all()
        work = await h.db.get(ProviderWork, work_id)
        assert work.state == "failed" and len(calls) == maximum
        assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "failed"
    else:
        assert work.state == "completed" and h.runs and work.prepared_deliveries
        assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "completed"
    assert (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id is None
    successful.close()
