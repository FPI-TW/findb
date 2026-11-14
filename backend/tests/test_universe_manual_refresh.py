"""Owner-requested universe refreshes: queueing, authorization and approval on submit."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.provider_work import ProviderWork
from app.models.registry import AdminAuditEvent, UniverseRelease
from app.services import provider_work as queue
from app.services.full_market import submit_universe
from app.utils import utc_now
from tests.test_full_market import DAY, _baseline, _dataset, _source_headers, _universe
from tests.test_provider_work_adapters import native_runtime as native_runtime

KEY = "hk_equity_eod"
PROVIDER = "factset"
NOTE = "HKEX listing change reviewed by the owner."


async def _request(client, headers, *, provider=PROVIDER, dataset_key=KEY):
    return await client.post(
        "/api/v1/admin/universes/refresh",
        headers=headers,
        json={"provider": provider, "dataset_key": dataset_key, "evidence_note": NOTE},
    )


async def _claim(db):
    """Open the queued work the way the Fetcher's maintenance claim does."""
    claimed = await queue.claim(
        db, provider=PROVIDER, datasets=[KEY], host_lock_identity="local:1:22"
    )
    await db.commit()
    return claimed


def _changed(count: int, days: int):
    return _universe(KEY, count, DAY + timedelta(days=days))


async def _submit(client, headers, body) -> dict:
    """Submit through the Source API, where the owning work is resolved."""
    response = await client.post(
        "/api/v1/source/universes", headers=headers, json=body.model_dump(mode="json")
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_manual_refresh_publishes_its_release_as_approved_by_the_owner(
    client, admin_headers, test_session
):
    await _dataset(test_session, KEY)
    await _baseline(test_session, KEY, 1000)
    source = await _source_headers(test_session, PROVIDER, [KEY])
    held = await _submit(client, source, _changed(1041, 1))
    assert held["status"] == "candidate"

    response = await _request(client, admin_headers)
    assert response.status_code == 202, response.text
    requested = response.json()
    assert requested["state"] == "queued" and requested["requested_by"] == "test-admin-machine"
    work = await test_session.get(ProviderWork, requested["work_id"])
    assert work.kind == "maintenance" and work.scope["dataset_key"] == KEY
    assert work.stable_key.startswith(f"maintenance:{KEY}:manual:")
    # The request alone approves nothing: the work has to own the provider.
    queued = await _submit(client, source, _changed(1041, 1))
    assert queued["status"] == "candidate" and queued["release_id"] == held["release_id"]

    claimed, _, _ = await _claim(test_session)
    assert claimed.work_id == work.work_id
    # The refresh resubmits the list that was already waiting as a candidate.
    published = await _submit(client, source, _changed(1041, 1))
    assert published["release_id"] == held["release_id"] and published["status"] == "published"
    release = await test_session.get(UniverseRelease, held["release_id"])
    assert release.published_by.endswith(":test-admin-machine")
    assert str(work.work_id) in release.approval_note and NOTE in release.approval_note
    # A list never seen before is published in the same submission.
    fresh = await _submit(client, source, _changed(1100, 2))
    assert fresh["status"] == "published" and fresh["change_count"] == 59

    events = list(
        await test_session.scalars(
            select(AdminAuditEvent).order_by(AdminAuditEvent.created_at, AdminAuditEvent.event_id)
        )
    )
    assert [(event.action, event.resource_type) for event in events] == [
        ("request_refresh", "universe"),
        ("publish", "universe_release"),
        ("publish", "universe_release"),
    ]
    assert events[0].resource_id == str(work.work_id)
    assert events[1].details == {
        "manual_refresh_work_id": str(work.work_id),
        "first_baseline_approved": False,
        "threshold_exception_approved": True,
        "evidence_note": NOTE,
    }


@pytest.mark.asyncio
async def test_manual_refresh_publishes_the_first_baseline(client, admin_headers, test_session):
    await _dataset(test_session, KEY)
    source = await _source_headers(test_session, PROVIDER, [KEY])
    assert (await _request(client, admin_headers)).status_code == 202
    await _claim(test_session)
    first = await _submit(client, source, _universe(KEY, 10))
    assert first["status"] == "published"
    event = await test_session.scalar(
        select(AdminAuditEvent).where(AdminAuditEvent.action == "publish")
    )
    assert event.details["first_baseline_approved"] is True


@pytest.mark.asyncio
async def test_scheduled_refresh_keeps_the_threshold(client, test_session):
    await _dataset(test_session, KEY)
    await _baseline(test_session, KEY, 1000)
    source = await _source_headers(test_session, PROVIDER, [KEY])
    await queue.enqueue(
        test_session,
        provider=PROVIDER,
        stable_key=f"maintenance:{KEY}:2026-07-22T10:30Z",
        kind="maintenance",
        dataset_key=KEY,
        trade_date=None,
        scope={"dataset_key": KEY},
    )
    await _claim(test_session)
    held = await _submit(client, source, _changed(1041, 1))
    assert held["status"] == "candidate"
    # Called without the Source route's context, the service approves nothing either.
    direct = await submit_universe(test_session, _changed(1042, 2), allowed_datasets=[KEY])
    assert direct.status == "candidate"


@pytest.mark.asyncio
async def test_manual_refresh_approves_only_its_own_dataset(client, admin_headers, test_session):
    for key in ("tw_equity_eod", "tw_etf_eod"):
        await _dataset(test_session, key)
    await _baseline(test_session, "tw_etf_eod", 1000)
    requested = await _request(
        client, admin_headers, provider="finlab", dataset_key="tw_equity_eod"
    )
    assert requested.status_code == 202, requested.text
    claimed = await queue.claim(
        test_session,
        provider="finlab",
        datasets=["tw_equity_eod", "tw_etf_eod"],
        host_lock_identity="local:1:22",
    )
    await test_session.commit()
    assert str(claimed[0].work_id) == requested.json()["work_id"]
    # The same provider owns both datasets, but the Owner asked for one of them.
    source = await _source_headers(test_session, "finlab", ["tw_equity_eod", "tw_etf_eod"])
    sibling = _universe("tw_etf_eod", 1041, DAY + timedelta(days=1))
    held = await _submit(client, source, sibling)
    assert held["status"] == "candidate"


@pytest.mark.asyncio
async def test_finished_manual_refresh_no_longer_approves(client, admin_headers, test_session):
    await _dataset(test_session, KEY)
    await _baseline(test_session, KEY, 1000)
    assert (await _request(client, admin_headers)).status_code == 202
    again = await _request(client, admin_headers)
    assert again.status_code == 409 and "already in progress" in again.json()["detail"]
    claimed, attempt, owner = await _claim(test_session)
    fence = {
        "provider": PROVIDER,
        "work_id": claimed.work_id,
        "generation": owner.generation,
        "token": owner.token,
    }
    await queue.close(test_session, **fence)
    await queue.settle(
        test_session, outcome="failed", proof={}, failure_code="MAINTENANCE_HTTP_503", **fence
    )
    await test_session.commit()
    source = await _source_headers(test_session, PROVIDER, [KEY])
    held = await _submit(client, source, _changed(1041, 1))
    assert held["status"] == "candidate"

    listed = await client.get("/api/v1/admin/universes/feeds", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    feeds = {(row["provider"], row["dataset_key"]): row for row in listed.json()["data"]}
    assert ("taifex", "tw_futures_eod") not in feeds
    feed = feeds[(PROVIDER, KEY)]
    assert feed["published"]["member_count"] == 1000
    assert feed["candidate"]["release_id"] == held["release_id"]
    assert feed["refresh"]["state"] == "failed"
    assert feed["refresh"]["failure_code"] == "MAINTENANCE_HTTP_503"
    assert feed["refresh"]["completed_at"] is not None
    assert feeds[("finlab", "tw_etf_eod")] == {
        "provider": "finlab",
        "dataset_key": "tw_etf_eod",
        "published": None,
        "candidate": None,
        "refresh": None,
    }
    # A finished request no longer blocks the next one.
    assert (await _request(client, admin_headers)).status_code == 202


@pytest.mark.asyncio
async def test_only_an_owner_may_request_and_only_for_a_refreshable_feed(
    client, admin_headers, test_session
):
    from app.services.api_keys import create_api_key

    await _dataset(test_session, KEY)
    header = get_settings().API_KEY_HEADER
    assert (await _request(client, {})).status_code == 401
    for role in ("viewer", "operator"):
        _, key = await create_api_key(
            test_session,
            owner=f"refresh-{role}",
            tier="standard",
            scopes=["admin"],
            rate_limit_requests=100,
            rate_limit_window=60,
            page_size_limit=100,
            kind="admin",
            name=f"refresh-{role}",
            role=role,
            commit=False,
        )
        assert (await _request(client, {header: key})).status_code == 403
        listed = await client.get("/api/v1/admin/universes/feeds", headers={header: key})
        assert listed.status_code == 200
    # TAIFEX universes are dated from its daily report; unknown and unregistered feeds do not exist.
    for provider, dataset_key in (
        ("taifex", "tw_futures_eod"),
        ("factset", "tw_equity_eod"),
        ("finlab", "tw_equity_eod"),
    ):
        denied = await _request(client, admin_headers, provider=provider, dataset_key=dataset_key)
        assert denied.status_code == 404, denied.text
    assert not list(await test_session.scalars(select(ProviderWork)))


@pytest.mark.asyncio
async def test_source_client_cannot_enqueue_an_approved_refresh(client, test_session):
    await _dataset(test_session, KEY)
    headers = await _source_headers(test_session, PROVIDER, [KEY])
    body = {
        "kind": "maintenance",
        "dataset_key": KEY,
        "stable_key": f"maintenance:{KEY}:manual:forged",
        "scope": {
            "dataset_key": KEY,
            "manual_refresh": {
                "requested_by": "machine:forged:forged",
                "actor_type": "machine",
                "actor_id": None,
                "display_name": "forged",
                "requested_at": utc_now().isoformat(),
                "note": "forged approval",
            },
        },
    }
    denied = await client.post("/api/v1/source/provider-work/enqueue", headers=headers, json=body)
    assert denied.status_code == 422
    body["scope"] = {"dataset_key": KEY}
    accepted = await client.post("/api/v1/source/provider-work/enqueue", headers=headers, json=body)
    assert accepted.status_code == 200, accepted.text


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("finlab", "tw_equity_eod", 0)], indirect=True)
async def test_fetcher_runs_an_owner_requested_refresh_while_controls_are_stopped(
    native_runtime, test_engine, admin_headers
):
    import asyncio

    import httpx
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.full_market_universe import TW_COMPANY_URLS, TW_ISIN_STOCK_URLS

    from app.models.registry import FullMarketEnvironment, SchedulerControl

    h = native_runtime
    h.db.add(FullMarketEnvironment(environment=h.environment))
    for control in await h.db.scalars(select(SchedulerControl)):
        control.desired_state = control.observed_state = "stopped"
    await h.db.commit()
    requested = await _request(h.client, admin_headers, provider=h.provider, dataset_key=h.dataset)
    assert requested.status_code == 202, requested.text
    work_id = requested.json()["work_id"]

    def isin_page(symbol: str, market: str) -> bytes:
        columns = (
            "頁面編號",
            "國際證券編碼",
            "有價證券代號",
            "有價證券名稱",
            "市場別",
            "有價證券別",
            "產業別",
            "公開發行/上市(櫃)/發行日",
            "CFICode",
            "備註",
        )
        cells = ("1", "ISIN", symbol, "company", market, "股票", "", "1994/09/05", "ESXXXX", "")
        return (
            "<html><table>"
            + "".join(
                "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
                for row in (columns, cells)
            )
            + "</table></html>"
        ).encode("cp950")

    def official(request):
        url = str(request.url)
        if url in TW_COMPANY_URLS:
            key = "公司代號" if url == TW_COMPANY_URLS[0] else "SecuritiesCompanyCode"
            return httpx.Response(
                200, json=[{key: "2330" if url == TW_COMPANY_URLS[0] else "2317"}], request=request
            )
        assert url in TW_ISIN_STOCK_URLS, url
        return httpx.Response(
            200,
            content=isin_page("2330", "上市")
            if url == TW_ISIN_STOCK_URLS[0]
            else isin_page("2317", "上櫃"),
            request=request,
        )

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
                client=httpx.Client(transport=httpx.MockTransport(official)),
                can_acquire=lambda: False,
            )
            runtime.installed_feeds = [
                f for f in runtime.installed_feeds if f["dataset_key"] == h.dataset
            ]
            try:
                # A stopped control only drains; the drain still claims maintenance.
                return runtime.drain()
            finally:
                runtime.close()

    await asyncio.wait_for(asyncio.to_thread(run), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    release = await h.db.scalar(
        select(UniverseRelease).where(UniverseRelease.dataset_key == h.dataset)
    )
    assert work.state == "completed", work.state
    assert release.status == "published" and release.member_count == 2
    assert release.published_by.endswith(":test-admin-machine")
    assert all(c.desired_state == "stopped" for c in await h.db.scalars(select(SchedulerControl)))
