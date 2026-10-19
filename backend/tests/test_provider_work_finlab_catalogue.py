import asyncio
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import FullMarketEnvironment, SchedulerControl
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("finlab", "tw_equity_eod", 0)], indirect=True)
@pytest.mark.parametrize("entry", ["explicit", "automatic"])
@pytest.mark.parametrize("fault", ["network", "config"])
async def test_finlab_native_catalogue_known_failure_settles(
    native_runtime, test_engine, monkeypatch, entry, fault
):
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.full_market_universe import TW_COMPANY_URLS, TW_ISIN_URLS

    h = native_runtime
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", True)
    h.db.add(FullMarketEnvironment(environment=h.environment, enabled=True))
    for control in await h.db.scalars(select(SchedulerControl)):
        control.desired_state = "stopped"
    await h.db.commit()
    from findb_fetcher.providers.finlab import FinLabConfigError

    original_native = h.native

    def fail_sdk(operation):
        original_native(operation)
        if operation[0] == "dataset":
            if fault == "config":
                raise FinLabConfigError("offline mapping denied")
            raise httpx.ReadError("offline FinLab catalogue network failure")

    h.native = fail_sdk
    if fault == "config":
        from findb_fetcher.providers.finlab import FinLabSdkGateway

        def invalid_config(*args, **kwargs):
            raise FinLabConfigError("offline mapping denied")

        monkeypatch.setattr(FinLabSdkGateway, "fetch_catalogue", invalid_config)
    queued = await h.client.post(
        "/api/v1/source/provider-work/enqueue",
        headers=h.headers,
        json={
            "kind": "maintenance",
            "dataset_key": h.dataset,
            "stable_key": f"maintenance:{h.dataset}:first",
        },
    )
    assert queued.status_code == 200
    work_id = UUID(queued.json()["work_id"])
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
    html = "<table><tr><td>股票</td></tr><tr><td>2330 company</td><td>x</td><td>x</td><td>x</td><td>x</td><td>ESXXXX</td></tr><tr><td>ETF</td></tr><tr><td>0050 etf</td><td>x</td><td>x</td><td>x</td><td>x</td><td>CEXXXX</td></tr></table>".encode(
        "cp950"
    )

    def official(request):
        url = str(request.url)
        if url in TW_COMPANY_URLS:
            key = "公司代號" if url == TW_COMPANY_URLS[0] else "SecuritiesCompanyCode"
            return httpx.Response(
                200, json=[{key: "2330" if url == TW_COMPANY_URLS[0] else "2317"}], request=request
            )
        assert url in TW_ISIN_URLS, url
        return httpx.Response(
            200,
            content=html
            if url == TW_ISIN_URLS[0]
            else html.replace(b"2330", b"2317").replace(b"0050", b"0056"),
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
            try:
                return (
                    runtime.sync_queued_universes()
                    if entry == "explicit"
                    else runtime._queued_cycle()
                )
            finally:
                runtime.close()

    await asyncio.wait_for(asyncio.to_thread(run), 30)
    h.db.expire_all()
    owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    work = await h.db.get(ProviderWork, work_id)
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.attempt_number == 1
        )
    )
    assert owner.attempt_id is None and owner.recovery_reason is None
    assert work.state == ("queued" if fault == "network" else "failed")
    assert attempt.outcome == ("retry" if fault == "network" else "failed")
    assert attempt.failure_code == (
        "MAINTENANCE_FINLAB_OPERATIONAL" if fault == "network" else "MAINTENANCE_FINLAB_CONFIG"
    )
    assert bool(h.native_calls()) == (fault == "network")
    assert not work.prepared_deliveries and not h.runs
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
