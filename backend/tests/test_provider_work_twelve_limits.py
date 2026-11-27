"""Dispatched native 429s retain historical member budget across queue restarts.

A frozen cohort date requests its members in one batch; each limited round spends one
attempt of the batch's first member, and a member whose budget runs out leaves the
date as missing while the others continue.
"""

import sqlite3
import time
from contextlib import closing
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import HistoricalBackfillRequest
from app.services.provider_work import prepared_map
from app.utils import utc_now
from tests.provider_work_clock import account_clock
from tests.test_provider_work_adapters import native_runtime as native_runtime

# Twelve Data/FinLab requests freeze a published Full release cohort.
pytestmark = pytest.mark.usefixtures("full_release_cohort")


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod")], indirect=True)
@pytest.mark.parametrize("fault", ["http429", "payload429"])
@pytest.mark.parametrize("recover", [False, True])
async def test_historical_native_rate_limit_preserves_budget_cooldown_and_scope_across_restart(
    native_runtime, test_engine, monkeypatch, fault, recover
):
    from findb_fetcher.provider_adapters import twelve_data as twelve_adapter
    from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig

    h = native_runtime
    offset = account_clock(monkeypatch)
    successful = twelve_adapter.TwelveDataClient(TwelveDataConfig("offline-twelve"))
    handler = successful._client._transport.handle_request
    calls = []
    from findb_fetcher.member_historical import MEMBER_ATTEMPTS

    maximum = MEMBER_ATTEMPTS

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

    # Frozen cohorts request each member through the Full daily adapter's client.
    monkeypatch.setattr(
        twelve_adapter,
        "TwelveDataClient",
        lambda config: TwelveDataClient(
            config, client=httpx.Client(transport=httpx.MockTransport(native))
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
        journal = h.queue.WorkJournal(
            h.path / f"account/provider-work-{h.provider}.sqlite3", str(work_id)
        )
        with journal.connection() as db:
            charged = db.execute(
                "SELECT attempts FROM work_budget WHERE work_key=? AND operation_key=?",
                (str(work_id), "member-historical:member:AAPL"),
            ).fetchone()[0]
        # One shared batch per round; the recovered round's dispatch counts too.
        assert charged == number and calls == ["AAPL,MSFT,NVDA"] * number
        if recover and number == maximum:
            break
        assert (
            work.state == "queued" and not (await prepared_map(h.db, work.work_id)) and not h.runs
        )
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
        # AAPL's budget is spent: it ends missing, and the rest of the date continues.
        assert await h.run()
        h.db.expire_all()
        work = await h.db.get(ProviderWork, work_id)
        assert work.state == "queued" and calls[-1] == "MSFT,NVDA" and len(calls) == maximum + 1
        with journal.connection() as db:
            budget = dict(
                db.execute(
                    "SELECT operation_key, attempts FROM work_budget WHERE work_key=?",
                    (str(work_id),),
                ).fetchall()
            )
            outcome = db.execute(
                "SELECT body FROM work_response WHERE work_key=? AND response_key=?",
                (str(work_id), "member-historical:outcome:AAPL"),
            ).fetchone()
        assert budget["member-historical:member:AAPL"] == maximum
        assert budget["member-historical:member:MSFT"] == 1
        assert outcome is not None and b'"outcome":"missing"' in outcome[0]
        assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "running"
    else:
        assert work.state == "completed" and h.runs and (await prepared_map(h.db, work.work_id))
        assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "completed"
    assert (
        await h.db.get(ProviderWorkOwner, (h.environment, h.provider, "default"))
    ).attempt_id is None
    successful.close()
