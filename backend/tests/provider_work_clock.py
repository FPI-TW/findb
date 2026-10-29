"""Control test clock coordinates while preserving authentic Source authorization."""

import time
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

from app.utils import utc_now


def recent_backfill_day() -> date:
    """Return a Thursday one to two weeks ago, so DAY + 1 is a weekday.

    Historical requests must stay inside the staging backfill window; a fixed
    calendar date silently ages out of it.
    """
    today = utc_now().date()
    return today - timedelta(days=7 + (today.weekday() - 3) % 7)


def dispatch_clock(monkeypatch):
    """Control grant receipt/dispatch timing independently of DB/ASGI latency."""
    from findb_fetcher import acquisition, full_market_client

    clock = [time.monotonic()]
    facade = SimpleNamespace(time=time.time, sleep=time.sleep, monotonic=lambda: clock[0])
    # Never patch the global time module: UTC, pacing, asyncio and bounded
    # harness execution retain their real clocks. The grant bounds are unchanged.
    monkeypatch.setattr(acquisition, "time", facade)
    monkeypatch.setattr(full_market_client, "time", facade)
    return clock


def account_clock(monkeypatch):
    from findb_fetcher import acquisition
    from findb_fetcher.full_market_client import FullMarketSourceClient

    offset = [0.0]
    from datetime import datetime, timedelta, timezone

    from findb_fetcher import provider_work as coordinator

    from app.services import provider_work as queue

    class QueueClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=offset[0])

    # The authoritative PG queue and its retry sender use the same advanced
    # clock as the governor; contract fetched_at validation remains real UTC.
    monkeypatch.setattr(
        queue, "utc_now", lambda: datetime.now(timezone.utc) + timedelta(seconds=offset[0])
    )
    monkeypatch.setattr(coordinator, "datetime", QueueClock)
    real_time = time.time
    monotonic = acquisition.time.monotonic
    monkeypatch.setattr(
        acquisition,
        "time",
        SimpleNamespace(
            time=lambda: real_time() + offset[0], monotonic=monotonic, sleep=time.sleep
        ),
    )
    authorize = FullMarketSourceClient.authorization

    def authorization(source, *args, **kwargs):
        # Source still verifies the actual key/scope/control/fence in ASGI.
        # Only its returned clock coordinates move with this test governor.
        grant = authorize(source, *args, **kwargs)
        return replace(
            grant,
            receipt_utc=grant.receipt_utc + offset[0],
            checked_at=grant.checked_at + offset[0],
            expires_at=grant.expires_at + offset[0] if grant.expires_at is not None else None,
        )

    monkeypatch.setattr(FullMarketSourceClient, "authorization", authorization)
    return offset


def shioaji_dispatch_clock(monkeypatch):
    """Keep nominal multi-member IPC dispatch in one controlled clock domain.

    The forked fake SDK checks the parent's monotonic grant deadline. It must
    inherit the same facade, including virtual account retry offsets. Real IPC
    waits and process termination retain their wall-clock timeouts.
    """
    from findb_fetcher import acquisition
    from findb_fetcher.providers import shioaji_control

    dispatch_clock(monkeypatch)
    offset = account_clock(monkeypatch)
    monkeypatch.setattr(shioaji_control, "time", acquisition.time)
    return offset


async def drain_retained_rerun_during_cooldown(h):
    """A seeded pre-cutover retained raw can normalize while new native I/O waits."""
    from datetime import timedelta
    from uuid import UUID

    from sqlalchemy import select

    from app.models.raw import RawMarketPayload
    from app.models.registry import IngestionRun, SourceClient
    from app.services.ingestion import payload_sha256
    from app.utils import utc_now, uuid7
    from tests.test_canonical_ingest_api import _canonical_request, _execute_run

    body = _canonical_request()["payload"]
    body["data"][0].update(
        symbol="COOLDOWN",
        source_symbol="COOLDOWN",
        currency="HKD" if h.dataset == "hk_equity_eod" else "USD",
    )
    raw_id, old_id = uuid7(), uuid7()
    client_id = await h.db.scalar(
        select(SourceClient.client_id).where(SourceClient.name == "native-matrix")
    )
    h.db.add(
        RawMarketPayload(
            raw_payload_id=raw_id,
            source_client_id=client_id,
            run_id=old_id,
            dataset_key=h.dataset,
            source=h.provider,
            request_key="retained-cooldown",
            idempotency_key="retained-cooldown",
            schema_id="market_eod",
            schema_version=1,
            payload=body,
            payload_sha256=payload_sha256(body),
            fetched_at=utc_now() - timedelta(days=1),
            expire_at=utc_now() + timedelta(days=30),
        )
    )
    h.db.add(
        IngestionRun(
            run_id=old_id,
            source_client_id=client_id,
            dataset_key=h.dataset,
            source=h.provider,
            raw_payload_id=raw_id,
            request_key="retained-cooldown",
            schema_id="market_eod",
            schema_version=1,
            status="failed",
            raw_records=1,
        )
    )
    await h.db.commit()
    result = await h.client.post(f"/api/v1/source/runs/{old_id}/rerun", headers=h.headers)
    assert result.status_code == 202, result.text
    new_id = UUID(result.json()["run_id"])
    calls = h.native_calls()
    with h.queue.host_lock(h.provider) as (_, inode):
        claim = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=h.headers,
            json={
                "protocol": "provider-work-v1",
                "host_lock_identity": inode,
                "accepted_kinds": ["rerun"],
            },
        )
        assert claim.status_code == 200, claim.text
        work = claim.json()["work"]
        assert work and work["kind"] == "rerun"
        fence = {"generation": work["generation"], "token": work["token"]}
        base = f"/api/v1/source/provider-work/{work['work_id']}"
        execute = await h.client.post(base + "/execute-rerun", headers=h.headers, json=fence)
        assert execute.status_code == 200, execute.text
        await _execute_run(h.db, h.engine, new_id)
        assert (
            await h.client.post(base + "/close", headers=h.headers, json=fence)
        ).status_code == 200
        settled = await h.client.post(
            base + "/settle", headers=h.headers, json={**fence, "outcome": "completed", "proof": {}}
        )
        assert settled.status_code == 200, settled.text
        waiting = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=h.headers,
            json={"protocol": "provider-work-v1", "host_lock_identity": inode},
        )
        assert waiting.status_code == 200 and waiting.json()["work"] is None
    h.db.expire_all()
    normalized = await h.db.get(IngestionRun, new_id)
    assert normalized.status == "completed" and normalized.success_records == 1
    assert (await h.db.get(IngestionRun, old_id)).status == "failed"
    assert h.native_calls() == calls
