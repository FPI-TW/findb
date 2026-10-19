import asyncio
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.canonical import CalendarYearRevision
from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import DatasetRegistry, FullMarketEnvironment
from app.services import provider_work
from tests.provider_work_runtime_support import DAY
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("taifex", "tw_futures_eod", 0)], indirect=True)
@pytest.mark.parametrize("failure_site", ["latest", "dated", "not-published", "quota", "permanent"])
async def test_daily_taifex_first_plan_known_native_failure_retries_original_work(
    native_runtime, test_engine, admin_headers, monkeypatch, failure_site
):
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState

    from tests import test_full_market as f
    from tests.full_market_fixtures import historical_admission

    h = native_runtime
    monkeypatch.setattr(f, "DAY", DAY)
    await f._calendar(h.db, open_dates=[DAY])
    original_universe = f._universe

    def universe(*args, **kwargs):
        body = original_universe(*args, **kwargs)
        first = body.members[0]
        body.members = [
            first.model_copy(
                update={
                    "symbol": product,
                    "provider_symbol": product + "202610F1",
                    "product_code": product,
                    "contract_code": product + ":202610F1",
                    "contract_month": "202610F1",
                    "sessions": ["regular"],
                }
            )
            for product in ["TX", "MTX", "TMF", "TE", "TF"]
        ]
        return body

    monkeypatch.setattr(f, "_universe", universe)
    _, release = await f._baseline(h.db, h.dataset, effective_date=DAY - timedelta(days=1))
    dataset = await h.db.get(DatasetRegistry, h.dataset)
    calendar = await h.db.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == "TAIFEX")
    )
    await historical_admission(h.db, dataset, release, DAY, calendar.id)
    get_settings().APP_ENVIRONMENT = h.environment
    h.db.add(FullMarketEnvironment(environment=h.environment, enabled=True))
    feed = {
        "provider": "taifex",
        "dataset_key": h.dataset,
        "market": "TW",
        "asset_class": "future",
        "slot_id": "taiwan_market_window",
        "source_timezone": "Asia/Taipei",
        "target_date_lag_days": (
            datetime.now(timezone.utc)
            .astimezone(__import__("zoneinfo").ZoneInfo("Asia/Taipei"))
            .date()
            - DAY
        ).days,
        "scheduled_time": "00:00",
    }
    work = await provider_work.enqueue(
        h.db,
        provider=h.provider,
        stable_key=f"daily:full_market:{h.dataset}:{DAY}",
        kind="daily",
        dataset_key=h.dataset,
        trade_date=DAY,
        execution_profile="full_market",
        scope={"feed": feed},
    )
    work_id = work.work_id
    await h.db.commit()
    following = await h.client.post(
        "/api/v1/source/provider-work/enqueue",
        headers=h.headers,
        json={
            "kind": "maintenance",
            "dataset_key": h.dataset,
            "stable_key": f"maintenance:{h.dataset}:next",
        },
    )
    assert following.status_code == 200
    from findb_fetcher import acquisition
    from findb_fetcher.platform_limits import account_identity

    from app.utils import utc_now
    from tests.provider_work_clock import account_clock

    offset = account_clock(monkeypatch)
    if failure_site == "quota":
        governor = FullMarketState(h.path / "account/governor.sqlite3")
        account = account_identity(h.provider, governor)
        limits = acquisition.limits
        monkeypatch.setattr(
            acquisition, "limits", lambda provider: {**limits(provider), "requests_per_day": 1}
        )
        with governor.connection() as db:
            db.execute(
                "INSERT INTO full_quota(account,window,requests,bytes) VALUES(?,?,?,0)",
                (account, acquisition._windows(time.time())[0], 100),
            )
    calls = []
    failing = True

    def native(request):
        calls.append(request.method)
        if request.method == "GET":
            if failing and failure_site in {"latest", "permanent"}:
                return httpx.Response(403 if failure_site == "permanent" else 503, request=request)
            return httpx.Response(200, content=b"old report", request=request)
        if failing and failure_site == "dated":
            return httpx.Response(503, request=request)
        values = parse_qs(request.content.decode())
        product = values["commodity_id"][0]
        day = DAY - timedelta(days=1) if failing and failure_site == "not-published" else DAY
        text = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,交易時段\n"
        text += f"{day.strftime('%Y/%m/%d')},{product},202610F1,100,110,90,105,37,-,-,一般\n"
        return httpx.Response(200, content=text.encode("cp950"), request=request)

    def run():
        with (
            FullMarketSourceClient(h.config, client=h.http()) as source,
            h.source_factory(h.config, h.registry) as delivery,
        ):
            runtime = FullMarketRuntime(
                provider=h.provider,
                config={"feeds": [feed]},
                state=FullMarketState(h.path / "daily.sqlite3"),
                source=source,
                delivery=delivery,
                raw_store=h.raw,
                client=httpx.Client(transport=httpx.MockTransport(native)),
            )
            try:
                return runtime._queued_cycle()
            finally:
                runtime.close()

    await asyncio.wait_for(asyncio.to_thread(run), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert (
        work.state == ("failed" if failure_site == "permanent" else "queued")
        and work.manifest is None
    )
    assert not work.prepared_deliveries and not h.runs
    owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    assert owner.attempt_id is None and owner.recovery_reason is None
    assert calls == (
        []
        if failure_site == "quota"
        else ["GET"]
        if failure_site in {"latest", "permanent"}
        else ["GET", "POST"]
    )
    with h.queue.host_lock(h.provider) as (_, inode):
        response = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=h.headers,
            json={
                "protocol": "provider-work-v1",
                "host_lock_identity": inode,
                "accepted_kinds": ["maintenance"],
            },
        )
        assert (
            response.status_code == 200
            and response.json()["work"]["work_id"] == following.json()["work_id"]
        )
        next_work = response.json()["work"]
        base = f"/api/v1/source/provider-work/{next_work['work_id']}"
        fence = {"generation": next_work["generation"], "token": next_work["token"]}
        assert (
            await h.client.post(base + "/close", headers=h.headers, json=fence)
        ).status_code == 200
        assert (
            await h.client.post(
                base + "/settle",
                headers=h.headers,
                json={**fence, "outcome": "failed", "proof": {}, "failure_code": "TEST_FINISHED"},
            )
        ).status_code == 200
    if failure_site == "permanent":
        return
    failing = False
    if failure_site == "quota":
        # Move eligibility to a new account window; never refund ledger history.
        offset[0] += 86401
        monkeypatch.setattr(acquisition, "limits", limits)
    continuation = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    continuation.not_before = utc_now() - timedelta(seconds=1)
    await h.db.commit()
    with FullMarketState(h.path / "daily.sqlite3").connection() as db:
        db.execute("UPDATE full_date_prerequisite SET next_at=0")
    await asyncio.wait_for(asyncio.to_thread(run), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    # The published dated membership remains an explicit Admin decision.
    from app.models.registry import UniverseRelease

    dated = await h.db.scalar(
        select(UniverseRelease).where(
            UniverseRelease.dataset_key == h.dataset, UniverseRelease.effective_date == DAY
        )
    )
    assert dated is not None and dated.status == "candidate"
    published = await h.client.post(
        f"/api/v1/admin/universes/{dated.release_id}/publish",
        headers=admin_headers,
        json={
            "evidence_note": "Verified five official dated product reports.",
            "threshold_exception_approved": True,
        },
    )
    assert published.status_code == 200, published.text
    calls_before_replay = list(calls)
    continuation = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    continuation.not_before = utc_now() - timedelta(seconds=1)
    await h.db.commit()
    with FullMarketState(h.path / "daily.sqlite3").connection() as db:
        db.execute("UPDATE full_date_prerequisite SET next_at=0")
    await asyncio.wait_for(asyncio.to_thread(run), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert calls == calls_before_replay, "valid native report cache must not be fetched again"
    assert work.state == "completed", (
        work.state,
        [
            (row.outcome, row.failure_code)
            for row in await h.db.scalars(
                select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work_id)
            )
        ],
        [event for event in h.source_events if event[1] >= 400],
    )
    assert work.manifest and h.runs
    assert (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id is None
