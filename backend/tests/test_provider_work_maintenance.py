"""Maintenance failures use the same durable FIFO round, including stopped controls."""

import asyncio
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import FullMarketEnvironment, SchedulerControl
from app.utils import utc_now
from tests.test_provider_work_adapters import native_runtime as native_runtime

TAIFEX_INVALID_REPORTS = {
    "format-empty": [],
    "format-row-string": ["invalid row"],
    "format-missing-date": [{}],
    "format-date-null": [{"Date": None}],
    "format-date-number": [{"Date": 20261015}],
    "format-date-container": [{"Date": {}}],
    "format-invalid-date": [{"Date": "2026/99/99"}],
    "format-bad-report": [{"Date": "2026/10/15"}],
    "format-inconsistent-dates": [{"Date": "2026/10/15"}, {"Date": "2026/10/16"}],
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime,fault",
    [
        (("twelve_data", "us_equity_eod", 0), fault)
        for fault in [503, "503-invalid", "transport", "quota", 403, "format"]
    ]
    + [
        ((provider, dataset, 0), fault)
        for provider, dataset in [
            ("finlab", "tw_equity_eod"),
            ("shioaji", "tw_equity_minute"),
            ("taifex", "tw_futures_eod"),
        ]
        for fault in [503, "503-invalid", "transport", "quota", 403, "format"]
    ]
    + [(("taifex", "tw_futures_eod", 0), fault) for fault in TAIFEX_INVALID_REPORTS],
    indirect=["native_runtime"],
)
@pytest.mark.parametrize("entry", ["explicit", "automatic"])
async def test_maintenance_known_failure_settles_and_next_work_claims_with_stopped_controls(
    native_runtime, test_engine, monkeypatch, entry, fault
):
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.provider_work import WorkJournal
    from findb_fetcher.providers.twelve_data import TwelveDataConfig

    h = native_runtime
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-twelve")
    get_settings().FULL_MARKET_ENABLED = True
    h.db.add(FullMarketEnvironment(environment=h.environment, enabled=True))
    for control in await h.db.scalars(select(SchedulerControl)):
        control.desired_state = "stopped"
    await h.db.commit()
    monkeypatch.setattr(
        TwelveDataConfig, "from_env", classmethod(lambda cls: TwelveDataConfig("offline-twelve"))
    )
    calls = []

    def provider(request):
        calls.append(request.url.path)
        if fault == "transport":
            raise httpx.ReadError("offline catalogue transport", request=request)
        malformed = isinstance(fault, str) and fault.startswith("format")
        return httpx.Response(
            200 if malformed else 503 if fault in {"quota", "503-invalid"} else fault,
            json=TAIFEX_INVALID_REPORTS.get(fault, {}) if malformed else {"status": "error"},
            headers={"Content-Length": "-1"} if fault == "503-invalid" else {},
            request=request,
        )

    if fault == "quota":
        from findb_fetcher.platform_limits import account_identity

        governor = FullMarketState(h.path / "account/governor.sqlite3")
        account = account_identity(
            h.provider,
            governor,
            credential="offline-twelve" if h.provider == "twelve_data" else None,
        )
        from findb_fetcher import acquisition

        platform_limits = acquisition.limits
        monkeypatch.setattr(
            acquisition,
            "limits",
            lambda provider: {**platform_limits(provider), "requests_per_day": 100},
        )

        with governor.connection() as db:
            db.execute(
                "INSERT INTO full_quota(account,window,requests,bytes) VALUES(?,?,?,0)",
                (account, acquisition._windows(time.time())[0], 100000000),
            )

    queued = await h.client.post(
        "/api/v1/source/provider-work/enqueue",
        headers=h.headers,
        json={
            "kind": "maintenance",
            "dataset_key": h.dataset,
            "stable_key": f"maintenance:{h.dataset}:{datetime.now(timezone.utc).date()}",
            "scope": {"dataset_key": h.dataset},
        },
    )
    assert queued.status_code == 200, queued.text
    work_id = queued.json()["work_id"]
    following = await h.client.post(
        "/api/v1/source/provider-work/enqueue",
        headers=h.headers,
        json={
            "kind": "maintenance",
            "dataset_key": h.dataset,
            "stable_key": f"maintenance:{h.dataset}:following",
        },
    )
    assert following.status_code == 200

    def run():
        with (
            FullMarketSourceClient(h.config, client=h.http()) as source,
            h.source_factory(h.config, h.registry) as delivery,
        ):
            runtime = FullMarketRuntime(
                provider=h.provider,
                config={"feeds": [{"provider": h.provider, "dataset_key": h.dataset}]},
                state=FullMarketState(h.path / "daily.sqlite3"),
                source=source,
                delivery=delivery,
                raw_store=h.raw,
                client=httpx.Client(transport=httpx.MockTransport(provider)),
                can_acquire=lambda: False,
            )
            try:
                return (
                    runtime.sync_queued_universes()
                    if entry == "explicit"
                    else runtime._queued_cycle()
                )
            finally:
                runtime.close()

    await asyncio.wait_for(asyncio.to_thread(run), 30)
    from uuid import UUID

    h.db.expire_all()
    work = await h.db.get(ProviderWork, UUID(work_id))
    assert work.state == (
        "queued" if fault in {503, "503-invalid", "transport", "quota"} else "failed"
    )
    owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    assert owner.attempt_id is None and owner.recovery_reason is None
    attempts = list(
        await h.db.scalars(
            select(ProviderWorkAttempt)
            .where(ProviderWorkAttempt.work_id == work.work_id)
            .order_by(ProviderWorkAttempt.attempt_number)
        )
    )
    assert attempts[0].state == "settled" and attempts[0].outcome == (
        "retry" if work.state == "queued" else "failed"
    )
    if fault in {503, "503-invalid"}:
        assert attempts[0].failure_code == "MAINTENANCE_HTTP_503"
    if isinstance(fault, str) and fault.startswith("format"):
        assert attempts[0].failure_code == "MAINTENANCE_FORMAT_OR_SCOPE"
        assert work.manifest is None and not work.prepared_deliveries
        assert attempts[0].proof["manual_pending_prepared"] == []
        assert "no_data" not in attempts[0].proof
    if work.state == "queued":
        assert (
            attempts[-1].not_before > utc_now()
            and attempts[-1].queue_order > attempts[0].queue_order
        )
    journal = WorkJournal(h.path / "account/provider-work.sqlite3", work_id)
    with journal.connection() as db:
        assert db.execute(
            "SELECT attempts FROM work_budget WHERE work_key=?", (work_id,)
        ).fetchone()[0] == (0 if fault == "quota" else 1)
    expected_calls = (
        0
        if fault == "quota"
        else 4
        if fault == "format" and h.provider in {"finlab", "shioaji"}
        else 1
    )
    assert len(calls) == expected_calls, (
        attempts[0].failure_code,
        h.source_events,
    )
    # No historical or daily cleaner is needed and the host descriptor is free.
    with h.queue.host_lock(h.provider) as (_, inode):
        claim = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=h.headers,
            json={
                "protocol": "provider-work-v1",
                "host_lock_identity": inode,
                "accepted_kinds": ["maintenance"],
            },
        )
        assert claim.status_code == 200, claim.text
        next_work = claim.json()["work"]
        assert next_work["work_id"] == following.json()["work_id"]
        fence = {"generation": next_work["generation"], "token": next_work["token"]}
        assert (
            await h.client.post(
                f"/api/v1/source/provider-work/{next_work['work_id']}/close",
                headers=h.headers,
                json=fence,
            )
        ).status_code == 200
        assert (
            await h.client.post(
                f"/api/v1/source/provider-work/{next_work['work_id']}/settle",
                headers=h.headers,
                json={**fence, "outcome": "failed", "failure_code": "TEST_FINISHED", "proof": {}},
            )
        ).status_code == 200
    if fault in {503, "quota"}:
        from findb_fetcher.full_market_state import FULL_WORK_MAX_ATTEMPTS

        rounds = 4 if fault == "quota" else FULL_WORK_MAX_ATTEMPTS
        for index in range(rounds):
            h.db.expire_all()
            continuation = await h.db.scalar(
                select(ProviderWorkAttempt).where(
                    ProviderWorkAttempt.work_id == UUID(work_id),
                    ProviderWorkAttempt.state == "queued",
                )
            )
            continuation.not_before = utc_now() - timedelta(seconds=1)
            await h.db.commit()
            await asyncio.wait_for(asyncio.to_thread(run), 30)
            h.db.expire_all()
            current = await h.db.get(ProviderWork, UUID(work_id))
            assert (
                await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
            ).attempt_id is None
            with journal.connection() as db:
                budget = db.execute(
                    "SELECT attempts FROM work_budget WHERE work_key=?", (work_id,)
                ).fetchone()[0]
            assert budget == (0 if fault == "quota" else min(index + 2, FULL_WORK_MAX_ATTEMPTS))
            assert current.state == ("failed" if fault == 503 and index == rounds - 1 else "queued")
        assert len(calls) == (0 if fault == "quota" else FULL_WORK_MAX_ATTEMPTS)
    assert all(c.desired_state == "stopped" for c in await h.db.scalars(select(SchedulerControl)))
