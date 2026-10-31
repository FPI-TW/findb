"""Daily native adapters stop a retryable round before another member/intent."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.provider_work import ProviderWork, ProviderWorkAttempt
from app.models.registry import (
    DailyDeliveryMember,
    DatasetRegistry,
    FullMarketEnvironment,
    SchedulerControl,
)
from app.services import provider_work
from app.services.provider_work import prepared_map
from app.utils import utc_now
from tests.provider_work_runtime_support import DAY
from tests.test_provider_work_adapters import native_runtime as native_runtime

# Contract months follow DAY so the monthly contract never expires before it.
CONTRACT_MONTHS = (f"{DAY:%Y%m}F1", f"{DAY.replace(day=1) + timedelta(days=32):%Y%m}")


DAILY_NATIVE_RUNTIMES = [
    ("twelve_data", "us_equity_eod"),
    ("twelve_data", "hk_equity_eod"),
    ("finlab", "tw_equity_eod"),
    ("finlab", "tw_etf_eod"),
    ("shioaji", "tw_equity_minute"),
    ("shioaji", "tw_etf_minute"),
    ("taifex", "tw_futures_eod"),
]
# Native rate-limit responses are Twelve Data payloads; other providers lack them.
DAILY_FAULT_CASES = [
    ((provider, dataset, 0), fault)
    for provider, dataset in DAILY_NATIVE_RUNTIMES
    for fault in [429, 503, "lost-ack", "native-network", "native-http429", "native-payload429"]
    if provider == "twelve_data" or fault not in {"native-http429", "native-payload429"}
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("native_runtime", "fault"), DAILY_FAULT_CASES, indirect=["native_runtime"]
)
async def test_daily_actual_native_failure_stops_round_and_next_attempt_completes_original_scope(
    native_runtime, test_engine, admin_headers, monkeypatch, fault
):
    from findb_fetcher import historical_runtime
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.platform_limits import limits

    from tests import test_full_market as fixtures

    h = native_runtime
    if h.provider == "shioaji":
        from findb_fetcher import acquisition, shioaji_recovery

        monkeypatch.setattr(shioaji_recovery, "time", acquisition.time)
    empty_response = fault == "native-empty"
    if empty_response:
        from findb_fetcher import acquisition, shioaji_recovery
        from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway

        monkeypatch.setattr(shioaji_recovery, "time", acquisition.time)
        original_run = PersistentIsolatedShioajiGateway._run
        observed = []

        def empty_once(gateway, kind, **command):
            value, before, after = original_run(gateway, kind, **command)
            if kind == "kbars":
                observed.append(command["symbol"])
                if len(observed) == 1:
                    value = {**value, "kbars": {key: [] for key in value["kbars"]}}
            return value, before, after

        monkeypatch.setattr(PersistentIsolatedShioajiGateway, "_run", empty_once)
    not_published = fault == "native-not-published"
    native_rate = fault in {"native-http429", "native-payload429"}
    if native_rate or not_published:
        from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig

        from tests.provider_work_clock import account_clock

        offset = account_clock(monkeypatch)

        successful = historical_runtime.TwelveDataClient(None)
        transport = successful._client._transport.handle_request
        observed_calls = []

        def rate_limited(request):
            h.native(["daily", request.url.params["symbol"]])
            observed_calls.append(request.url.params["symbol"])
            if not_published:
                response = transport(request)
                if len(observed_calls) == 1:
                    payload = response.json()
                    payload["values"][0]["datetime"] = (DAY - timedelta(days=1)).isoformat()
                    return httpx.Response(200, json=payload, request=request)
                return response
            return httpx.Response(
                429 if fault == "native-http429" else 200,
                json={"status": "error", "code": 429},
                headers={"Retry-After": "120"},
                request=request,
            )

        monkeypatch.setattr(
            historical_runtime,
            "TwelveDataClient",
            lambda config: TwelveDataClient(
                TwelveDataConfig("offline-twelve"),
                client=httpx.Client(transport=httpx.MockTransport(rate_limited)),
            ),
        )
    assert h.request_id is None, "daily-only feeds must not create a historical request"

    async def dataset(db, key):
        return await db.get(DatasetRegistry, key)

    universe = fixtures._universe

    def native_universe(key, count=2, effective_date=DAY):
        body = universe(key, count, effective_date=DAY)
        symbols = (
            ("00700", "00005")
            if key == "hk_equity_eod"
            else (
                ("AAPL", "MSFT")
                if h.provider == "twelve_data"
                else ("0050", "0056")
                if key in {"tw_etf_minute", "tw_etf_eod"}
                else ("2330", "2317")
            )
        )
        for index, member in enumerate(body.members):
            if h.provider == "taifex":
                month = CONTRACT_MONTHS[index]
                member.provider_symbol, member.contract_code, member.contract_month = (
                    "TX" + month,
                    "TX:" + month,
                    month,
                )
            else:
                member.symbol = member.provider_symbol = symbols[index]
                if h.provider == "twelve_data":
                    member.exchange = "XHKG" if key == "hk_equity_eod" else "XNAS"
        return body

    monkeypatch.setattr(fixtures, "_dataset", dataset)
    monkeypatch.setattr(fixtures, "_universe", native_universe)
    monkeypatch.setattr(fixtures, "DAY", DAY)
    _, _, db_plan = await fixtures._activated_plan(h.db, h.dataset, count=2)
    get_settings().APP_ENVIRONMENT = h.environment
    h.db.add(FullMarketEnvironment(environment=h.environment, enabled=True))
    feed = {
        "provider": h.provider,
        "dataset_key": h.dataset,
        "market": "HK"
        if h.dataset == "hk_equity_eod"
        else "US"
        if h.provider == "twelve_data"
        else "TW",
        "asset_class": "future"
        if h.provider == "taifex"
        else "etf"
        if h.dataset in {"tw_etf_minute", "tw_etf_eod"}
        else "equity",
        "slot_id": "western_markets_window"
        if h.provider == "twelve_data"
        else "taiwan_market_window",
        "source_timezone": "Asia/Hong_Kong"
        if h.dataset == "hk_equity_eod"
        else "America/New_York"
        if h.provider == "twelve_data"
        else "Asia/Taipei",
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
    handle, native = h.handle, h.native
    injected = False
    calls_at_failure = None

    def source_fault(request):
        nonlocal injected, calls_at_failure
        if (
            fault in {429, 503, "lost-ack"}
            and request.url.path == "/api/v1/source/ingest"
            and not injected
        ):
            injected = True
            calls_at_failure = h.native_calls()
            if fault == "lost-ack":
                handle(request)
                raise httpx.ReadError("lost accepted acknowledgement")
            return httpx.Response(fault, request=request)
        return handle(request)

    def native_fault(operation):
        nonlocal injected, calls_at_failure
        native(operation)
        if (
            fault == "native-network"
            and not injected
            and operation[0] in {"daily", "dataset", "kbars", "report"}
        ):
            # Child processes use the trace as the durable once-only fault marker.
            if (
                sum(
                    json.loads(row)[0] in {"daily", "dataset", "kbars", "report"}
                    for row in h.native_calls()
                )
                == 1
            ):
                injected = True
                calls_at_failure = h.native_calls()
                raise httpx.ReadError("dispatched offline native network fault")

    h.handle, h.native = source_fault, native_fault
    state_path = h.path / "daily.sqlite3"
    acquired_members = []

    def report(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"not the target report")
        values = parse_qs(request.content.decode())
        product = values["commodity_id"][0]
        h.native(["report", product])
        text = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,交易時段\n"
        for month in CONTRACT_MONTHS:
            for session in ("一般", "盤後"):
                text += f"{DAY:%Y/%m/%d},{product},{month},100,110,90,105,37,-,-,{session}\n"
        return httpx.Response(200, content=text.encode("cp950"))

    def run_round():
        state = FullMarketState(state_path)
        state.bind_profile("full_market")
        with (
            FullMarketSourceClient(
                h.config, client=h.http(), execution_profile="full_market"
            ) as source,
            h.source_factory(h.config, h.registry) as delivery,
        ):
            runtime = FullMarketRuntime(
                provider=h.provider,
                config={"feeds": [feed]},
                state=state,
                source=source,
                delivery=delivery,
                raw_store=h.raw,
                execution_profile="full_market",
                client=httpx.Client(transport=httpx.MockTransport(report)),
            )
            runtime.proof = limits(h.provider)
            if h.provider == "twelve_data":
                runtime.td = historical_runtime.TwelveDataClient(None)
            acquire = runtime._acquire

            def traced(member, *args):
                acquired_members.append(member["member_key"])
                return acquire(member, *args)

            runtime._acquire = traced
            try:
                with h.queue.claimed_work(source, h.provider, accepted_kinds=["daily"]) as owner:
                    assert owner
                    plan = source.evaluate(str(db_plan.plan_id))
                    owner.freeze(
                        {
                            "mode": "canonical",
                            "plan_id": str(db_plan.plan_id),
                            "members": [
                                {"key": key}
                                for part in plan["parts"]
                                for key in part["member_keys"]
                            ],
                        }
                    )
                    state.record_plan(plan, h.provider)
                    release = source.release(str(db_plan.release_id))
                    if h.provider == "shioaji":
                        release = runtime._install_shioaji_repairs(owner, plan, release)
                    result = runtime._deliver_plan(plan, release, feed)
                    runtime._settle_queued_round(owner)
                    return result
            finally:
                runtime.close()

    assert not await asyncio.wait_for(asyncio.to_thread(run_round), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "queued"
    frozen, descriptors = (
        work.manifest,
        json.loads(json.dumps((await prepared_map(h.db, work.work_id)))),
    )
    assert len(acquired_members) == 1, (
        "a retryable round cannot acquire another frozen member",
        h.source_events,
    )
    assert len(descriptors) == (
        0 if fault == "native-network" or native_rate or not_published or empty_response else 1
    )
    if calls_at_failure is not None:
        assert h.native_calls() == calls_at_failure
    first_attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.attempt_number == 1
        )
    )
    assert first_attempt.outcome == "retry"
    if fault == "native-network":
        reported = list(
            await h.db.scalars(
                select(DailyDeliveryMember).where(
                    DailyDeliveryMember.plan_id == db_plan.plan_id,
                    DailyDeliveryMember.outcome_reason == "source_error",
                )
            )
        )
        if h.provider == "taifex":
            # One exchange report serves every member, so an outage blocks no member.
            assert reported == []
        else:
            assert len(reported) == 1
            assert reported[0].outcome == "blocked"
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    if native_rate:
        if fault == "native-http429":
            from tests.provider_work_clock import drain_retained_rerun_during_cooldown

            assert attempt.not_before > utc_now() + timedelta(seconds=110)
            await drain_retained_rerun_during_cooldown(h)
            work = await h.db.get(ProviderWork, work_id)
            attempt = await h.db.scalar(
                select(ProviderWorkAttempt).where(
                    ProviderWorkAttempt.work_id == work_id,
                    ProviderWorkAttempt.state == "queued",
                )
            )
        from findb_fetcher.full_market_state import FULL_WORK_MAX_ATTEMPTS

        from app.models.provider_work import ProviderWorkOwner

        governor = h.path / "account/governor.sqlite3"
        for number in range(1, FULL_WORK_MAX_ATTEMPTS + 1):
            # Each run_round creates fresh runtime/state/client instances.
            with FullMarketState(state_path).connection() as local:
                row = local.execute(
                    "SELECT attempts,status FROM full_work WHERE member_key=?",
                    (acquired_members[0],),
                ).fetchone()
            assert row["attempts"] == number
            with closing(sqlite3.connect(governor)) as ledger:
                dispatched = ledger.execute(
                    "SELECT COUNT(*) FROM acquisition_intent WHERE actual_start IS NOT NULL"
                ).fetchone()[0]
                floor = ledger.execute("SELECT MAX(next_at) FROM full_pacing").fetchone()[0]
            assert dispatched == number and floor > time.time() + offset[0]
            assert not (await prepared_map(h.db, work.work_id)) and work.manifest == frozen
            if number == FULL_WORK_MAX_ATTEMPTS:
                break
            offset[0] += 121
            attempt.not_before = utc_now() - timedelta(seconds=1)
            await h.db.commit()
            with FullMarketState(state_path).connection() as local:
                local.execute("UPDATE full_work SET next_at=0")
            assert not await asyncio.wait_for(asyncio.to_thread(run_round), 30)
            h.db.expire_all()
            work = await h.db.get(ProviderWork, work_id)
            attempt = await h.db.scalar(
                select(ProviderWorkAttempt).where(
                    ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
                )
            )
        assert work.state == "failed"
        assert not (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id
        assert len(acquired_members) == FULL_WORK_MAX_ATTEMPTS
        return
    if not_published:
        import hashlib

        from findb_fetcher.provider_work import WorkJournal

        journal = WorkJournal(h.path / "account/provider-work.sqlite3", str(work_id))
        with journal.connection() as db:
            negative = db.execute(
                "SELECT body,sha256 FROM work_response_observation WHERE work_key=?",
                (str(work_id),),
            ).fetchall()
        assert len(negative) == 1 and hashlib.sha256(negative[0][0]).hexdigest() == negative[0][1]
        with FullMarketState(state_path).connection() as db:
            budget_before = db.execute(
                "SELECT attempts FROM full_work WHERE member_key=?", (acquired_members[0],)
            ).fetchone()[0]
        assert budget_before == 1
    if empty_response:
        with FullMarketState(state_path).connection() as db:
            assert (
                db.execute(
                    "SELECT attempts FROM full_work WHERE member_key=?", (acquired_members[0],)
                ).fetchone()[0]
                == 0
            )
    if h.provider == "shioaji" and (empty_response or fault == "native-network"):
        h.shioaji_clock[0] += 901
    attempt.not_before = utc_now() - timedelta(seconds=1)
    await h.db.commit()
    with FullMarketState(state_path).connection() as db:
        db.execute("UPDATE full_work SET next_at=0")
    h.handle, h.native = handle, native
    assert await asyncio.wait_for(asyncio.to_thread(run_round), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "completed" and work.manifest == frozen
    if empty_response:
        assert len(observed) == 3 and observed[0] == observed[1]
        assert len(h.runs) == 2
    saved = await prepared_map(h.db, work.work_id)
    assert all(saved[key]["body"] == value["body"] for key, value in descriptors.items())
    if not_published:
        assert observed_calls == (
            ["AAPL", "AAPL", "MSFT"]
            if h.dataset == "us_equity_eod"
            else ["00005", "00005", "00700"]
        )
        with journal.connection() as db:
            assert (
                db.execute(
                    "SELECT body,sha256 FROM work_response_observation WHERE work_key=?",
                    (str(work_id),),
                ).fetchall()
                == negative
            )
        with FullMarketState(state_path).connection() as db:
            assert (
                db.execute(
                    "SELECT attempts FROM full_work WHERE member_key=?", (acquired_members[0],)
                ).fetchone()[0]
                == budget_before + 1
            )
    assert all(
        row.desired_state == "running"
        for row in await h.db.scalars(
            select(SchedulerControl).where(SchedulerControl.provider == h.provider)
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime",
    [("twelve_data", "us_equity_eod", 0), ("twelve_data", "hk_equity_eod", 0)],
    indirect=True,
)
async def test_daily_target_not_published_refetches_original_operation_after_restart(
    native_runtime, test_engine, admin_headers, monkeypatch
):
    await test_daily_actual_native_failure_stops_round_and_next_attempt_completes_original_scope(
        native_runtime, test_engine, admin_headers, monkeypatch, "native-not-published"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime",
    [("shioaji", "tw_equity_minute", 0), ("shioaji", "tw_etf_minute", 0)],
    indirect=True,
)
async def test_shioaji_empty_native_observation_later_completes_original_scope(
    native_runtime, test_engine, admin_headers, monkeypatch
):
    await test_daily_actual_native_failure_stops_round_and_next_attempt_completes_original_scope(
        native_runtime, test_engine, admin_headers, monkeypatch, "native-empty"
    )
