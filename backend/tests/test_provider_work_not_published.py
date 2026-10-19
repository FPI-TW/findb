import hashlib
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkAttempt
from app.utils import utc_now
from tests.provider_work_runtime_support import DAY
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod")], indirect=True)
async def test_historical_target_not_published_observation_allows_fresh_native_after_restart(
    native_runtime, test_engine, monkeypatch
):
    from findb_fetcher import historical_runtime
    from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig

    h = native_runtime
    successful = historical_runtime.TwelveDataClient(None)
    handler = successful._client._transport.handle_request
    calls = []

    def native(request):
        calls.append(request.url.params["symbol"])
        response = handler(request)
        if len(calls) == 1:
            payload = response.json()
            payload["values"][0]["datetime"] = (DAY - timedelta(days=1)).isoformat()
            return httpx.Response(200, json=payload, request=request)
        return response

    monkeypatch.setattr(
        historical_runtime,
        "TwelveDataClient",
        lambda config: TwelveDataClient(
            TwelveDataConfig("offline-twelve"),
            client=httpx.Client(transport=httpx.MockTransport(native)),
        ),
    )
    assert await h.run()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    work_id = work.work_id
    assert work.state == "queued" and not work.prepared_deliveries and not h.runs
    frozen = work.manifest
    journal = h.queue.WorkJournal(h.path / "account/provider-work.sqlite3", str(work_id))
    with journal.connection() as db:
        rejected = db.execute(
            "SELECT body,sha256,origin FROM work_response_observation WHERE work_key=?",
            (str(work_id),),
        ).fetchall()
        before = db.execute(
            "SELECT SUM(attempts) FROM work_budget WHERE work_key=?", (str(work_id),)
        ).fetchone()[0]
    assert len(rejected) == 1 and rejected[0][2] == "native"
    assert hashlib.sha256(rejected[0][0]).hexdigest() == rejected[0][1]
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    attempt.not_before = utc_now() - timedelta(seconds=1)
    await h.db.commit()
    assert await h.run()
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "completed" and work.manifest == frozen and h.runs
    assert calls == [frozen["members"][0]["symbol"], *[row["symbol"] for row in frozen["members"]]]
    with journal.connection() as db:
        assert (
            db.execute(
                "SELECT SUM(attempts) FROM work_budget WHERE work_key=?", (str(work_id),)
            ).fetchone()[0]
            >= before
            >= 1
        )
        assert (
            db.execute(
                "SELECT body,sha256,origin FROM work_response_observation WHERE work_key=?",
                (str(work_id),),
            ).fetchall()
            == rejected
        )
    successful.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("finlab", "tw_equity_eod")], indirect=True)
async def test_finlab_native_negative_table_survives_restart_and_completes_frozen_members(
    native_runtime, test_engine, monkeypatch
):
    from dataclasses import replace
    from types import SimpleNamespace

    from findb_fetcher.providers.finlab import FinLabSdkGateway

    h = native_runtime
    factory = FinLabSdkGateway.from_env
    calls = []

    def gateway(cls, **kwargs):
        original = factory(**kwargs)
        sdk = original._sdk

        def get(name):
            calls.append(name)
            frame = sdk.data.get(name)
            if len(calls) == 1:
                frame.index = type(frame.index)(day - timedelta(days=1) for day in frame.index)
            return frame

        return replace(
            original, _sdk=SimpleNamespace(login=sdk.login, data=SimpleNamespace(get=get))
        )

    monkeypatch.setattr(FinLabSdkGateway, "from_env", classmethod(gateway))
    assert await h.run()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    work_id, frozen = work.work_id, work.manifest
    assert work.state == "queued" and not work.prepared_deliveries and not h.runs
    journal = h.queue.WorkJournal(h.path / "account/provider-work.sqlite3", str(work_id))
    with journal.connection() as db:
        rejected = db.execute(
            "SELECT body,sha256,origin FROM work_response_observation WHERE work_key=?",
            (str(work_id),),
        ).fetchall()
        before = db.execute(
            "SELECT SUM(attempts) FROM work_budget WHERE work_key=?", (str(work_id),)
        ).fetchone()[0]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM work_response WHERE work_key=?", (str(work_id),)
            ).fetchone()[0]
            == 0
        )
    assert len(rejected) == 1 and rejected[0][2] == "native"
    assert hashlib.sha256(rejected[0][0]).hexdigest() == rejected[0][1]
    retry = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    retry.not_before = utc_now() - timedelta(seconds=1)
    await h.db.commit()
    assert await h.run()
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "completed" and work.manifest == frozen and h.runs
    assert len(calls) == 6  # One negative, then the original five dataset fields.
    with journal.connection() as db:
        assert (
            db.execute(
                "SELECT body,sha256,origin FROM work_response_observation WHERE work_key=?",
                (str(work_id),),
            ).fetchall()
            == rejected
        )
        assert (
            db.execute(
                "SELECT SUM(attempts) FROM work_budget WHERE work_key=?", (str(work_id),)
            ).fetchone()[0]
            >= before
            >= 1
        )
