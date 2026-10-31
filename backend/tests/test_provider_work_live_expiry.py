"""An active owner cannot bypass the provider-local historical expiry boundary."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.models.provider_work import ProviderWorkOwner
from app.models.registry import HistoricalBackfillItem
from app.services import historical_backfill as historical
from app.services import provider_work as queue
from app.services.provider_work import prepared_map
from tests.provider_work_runtime_support import DAY, SCOPES
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", SCOPES, indirect=True)
async def test_active_historical_authorization_checks_exact_expiry_without_mutating_lease(
    native_runtime, test_engine, monkeypatch
):
    h = native_runtime
    with h.queue.host_lock(h.provider) as (_, inode):
        response = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=h.headers,
            json={"protocol": "provider-work-v1", "host_lock_identity": inode},
        )
        assert response.status_code == 200, response.text
        work = response.json()["work"]
        fence = {"generation": work["generation"], "token": work["token"]}
        work_headers = {
            **h.headers,
            "X-FinDB-Work-Id": work["work_id"],
            "X-FinDB-Work-Generation": str(work["generation"]),
            "X-FinDB-Work-Token": work["token"],
        }
        claimed = await h.client.post(
            "/api/v1/source/historical-backfills/claim", headers=work_headers
        )
        assert claimed.status_code == 200, claimed.text
        first_expired_date = DAY + timedelta(
            days=historical._max_backfill_days() + (h.provider != "taifex")
        )
        boundary = datetime.combine(
            first_expired_date, time(), ZoneInfo(historical.PROVIDER_TIMEZONES[h.provider])
        ).astimezone(timezone.utc)
        current = [boundary - timedelta(microseconds=1)]
        monkeypatch.setattr(queue, "utc_now", lambda: current[0])
        monkeypatch.setattr(historical, "utc_now", lambda: current[0])
        owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
        owner.heartbeat_at, owner.expires_at = boundary, boundary + timedelta(minutes=2)
        await h.db.commit()
        item = await h.db.get(HistoricalBackfillItem, h.item_id)
        original = (item.status, item.lease_token, item.lease_expires_at)
        route = f"/api/v1/source/provider-work/{work['work_id']}"
        for expired in (False, True):
            current[0] = boundary if expired else boundary - timedelta(microseconds=1)
            account = await h.client.post(
                f"/api/v1/source/historical-backfills/{h.item_id}/authorization",
                headers=h.headers,
                json={"lease_token": work["token"]},
            )
            assert account.status_code == 200, account.text
            assert account.json()["acquisition_allowed"] is not expired
            # Read-only projection returns the owner expiry, never renews item.
            await h.db.commit()
            await h.db.refresh(item)
            assert (item.status, item.lease_token, item.lease_expires_at) == original
            authorized = await h.client.post(route + "/authorize", headers=h.headers, json=fence)
            assert authorized.status_code == (409 if expired else 200), authorized.text
            if not expired:
                evidence = authorized.json()
                assert evidence["work_id"] == work["work_id"]
                assert evidence["provider"] == h.provider
                assert {key: evidence[key] for key in fence} == fence
                assert datetime.fromisoformat(evidence["checked_at"]) == current[0]
                assert datetime.fromisoformat(evidence["expires_at"]) == boundary + timedelta(
                    minutes=2
                )
                await h.db.refresh(owner)
                # Authorization projects current expiry; it never renews the owner.
                assert owner.heartbeat_at == boundary
                assert owner.expires_at == boundary + timedelta(minutes=2)
        manifest = await h.client.post(
            route + "/manifest",
            headers=h.headers,
            json={**fence, "manifest": {"mode": "canonical", "members": [{"key": "original"}]}},
        )
        assert manifest.status_code == 409
        assert manifest.json()["detail"]["code"] == "PROVIDER_WORK_SUSPENDED"
        # The authenticated write path applies the terminal domain decision;
        # an expiry denial itself must not leave an open owner forever.
        completed = await h.client.post(
            f"/api/v1/source/historical-backfills/{h.item_id}/complete",
            headers=work_headers,
            json={
                "lease_token": work["token"],
                "status": "failed",
                "failure_code": "ACQUISITION_SUSPENDED",
            },
        )
        assert completed.status_code == 200, completed.text
        h.db.expire_all()
        item = await h.db.get(HistoricalBackfillItem, h.item_id)
        assert item.status == "failed" and item.failure_code == "BACKFILL_EXPIRED"
        owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
        assert owner.attempt_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod")], indirect=True)
async def test_owner_expiry_blocks_new_intent_but_drains_original_prepared_body(
    native_runtime, test_engine, monkeypatch
):
    import asyncio
    import json

    from sqlalchemy import select

    from app.models.provider_work import ProviderWork
    from app.models.registry import HistoricalBackfillRequest

    h = native_runtime
    h.confirm_barrier = True
    task = asyncio.create_task(h.run())
    try:
        await asyncio.wait_for(h.prepared.wait(), 15)
        calls = h.native_calls()
        work = await h.db.scalar(
            select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
        )
        work_id = work.work_id
        descriptor = next(iter((await prepared_map(h.db, work.work_id)).values()))
        body = descriptor["body"]
        owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
        later = queue.utc_now() + timedelta(days=90)
        owner.expires_at = later + timedelta(minutes=2)
        owner.heartbeat_at = later
        fence = {"generation": owner.generation, "token": str(owner.token)}
        await h.db.commit()
        monkeypatch.setattr(queue, "utc_now", lambda: later)
        monkeypatch.setattr(historical, "utc_now", lambda: later)
        invented = json.loads(body)
        invented["idempotency_key"] = "new-after-expiry"
        result = await h.client.post(
            f"/api/v1/source/provider-work/{work_id}/prepare",
            headers=h.headers,
            json={**fence, "stage": "intent", "body": json.dumps(invented)},
        )
        assert result.status_code == 409, result.text
        h.allow_confirm.set()
        assert await asyncio.wait_for(task, 15)
        h.db.expire_all()
        item = await h.db.get(HistoricalBackfillItem, h.item_id)
        request = await h.db.get(HistoricalBackfillRequest, h.request_id)
        assert item.status == request.status == "failed"
        assert item.failure_code == request.failure_code == "BACKFILL_EXPIRED"
        work = await h.db.get(ProviderWork, work_id)
        assert next(iter((await prepared_map(h.db, work.work_id)).values()))["body"] == body
        assert h.native_calls() == calls and h.runs
        assert (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id is None
    finally:
        h.allow_confirm.set()
        if not task.done():
            await asyncio.wait_for(task, 15)


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod")], indirect=True)
async def test_owner_authorization_projects_latest_heartbeat_expiry(
    native_runtime, test_engine, monkeypatch
):
    h = native_runtime
    with h.queue.host_lock(h.provider) as (_, inode):
        claim = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=h.headers,
            json={"protocol": "provider-work-v1", "host_lock_identity": inode},
        )
        assert claim.status_code == 200, claim.text
        work = claim.json()["work"]
        fence = {"generation": work["generation"], "token": work["token"]}
        route = f"/api/v1/source/provider-work/{work['work_id']}"
        later = queue.utc_now() + timedelta(seconds=10)
        monkeypatch.setattr(queue, "utc_now", lambda: later)
        heartbeat = await h.client.post(route + "/heartbeat", headers=h.headers, json=fence)
        assert heartbeat.status_code == 200, heartbeat.text
        authorized = await h.client.post(route + "/authorize", headers=h.headers, json=fence)
        assert authorized.status_code == 200, authorized.text
        evidence = authorized.json()
        assert datetime.fromisoformat(evidence["checked_at"]) == later
        assert datetime.fromisoformat(evidence["expires_at"]) == later + queue.HEARTBEAT_TTL
        assert datetime.fromisoformat(evidence["expires_at"]) > datetime.fromisoformat(
            work["expires_at"]
        )
        owner = await h.db.get(
            ProviderWorkOwner, (h.environment, h.provider), populate_existing=True
        )
        assert owner.heartbeat_at == later
        assert owner.expires_at == later + queue.HEARTBEAT_TTL
