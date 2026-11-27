import hashlib
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkAttempt
from app.models.registry import HistoricalBackfillItem
from app.services.provider_work import prepared_map
from app.utils import utc_now
from tests.provider_work_runtime_support import DAY
from tests.test_provider_work_adapters import native_runtime as native_runtime

# Twelve Data/FinLab requests freeze a published Full release cohort.
pytestmark = pytest.mark.usefixtures("full_release_cohort")


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod")], indirect=True)
async def test_a_date_twelve_data_has_not_published_settles_partial_with_every_member_missing(
    native_runtime, test_engine, monkeypatch
):
    """A date the provider has not published yet settles partial; it does not wait."""
    from findb_fetcher.provider_adapters import twelve_data as twelve_adapter
    from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig

    h = native_runtime
    successful = twelve_adapter.TwelveDataClient(TwelveDataConfig("offline-twelve"))
    handler = successful._client._transport.handle_request
    calls = []

    def native(request):
        calls.append(request.url.params["symbol"])
        payload = handler(request).json()
        # Twelve Data still serves the previous session for every symbol of the batch.
        for series in [payload] if "values" in payload else payload.values():
            series["values"][0]["datetime"] = (DAY - timedelta(days=1)).isoformat()
        return httpx.Response(200, json=payload, request=request)

    monkeypatch.setattr(
        twelve_adapter,
        "TwelveDataClient",
        lambda config: TwelveDataClient(
            config, client=httpx.Client(transport=httpx.MockTransport(native))
        ),
    )
    assert await h.run()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    item = await h.db.get(HistoricalBackfillItem, h.item_id)
    assert work.state == item.status == "partial" and not h.runs
    # One shared batch, and every frozen member is missing for want of a source row.
    assert calls == ["AAPL,MSFT,NVDA"]
    assert {
        (row["member_key"], row["outcome"], row.get("reason"))
        for row in item.result_evidence["members"]
    } == {(key, "missing", "no_source_row") for key in ("AAPL", "MSFT", "NVDA")}
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
    assert work.state == "queued" and not (await prepared_map(h.db, work.work_id)) and not h.runs
    journal = h.queue.WorkJournal(
        h.path / f"account/provider-work-{h.provider}.sqlite3", str(work_id)
    )
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
