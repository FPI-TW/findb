"""Source-first dated TAIFEX prerequisites preserve frozen work and quotas."""

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

import findb_fetcher.full_market_runtime as runtime_module
from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.full_market_runtime import DatedUniversePendingError
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.providers.taifex import TaifexOperationalError, fetch_report
from test_full_market import _runtime

DAY = date(2026, 7, 22)
CODE = "taifex_dated_universe_required"


def setup_runtime(tmp_path, monkeypatch, profile):
    fixed = datetime(2026, 7, 22, 23, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(runtime_module, "datetime", Clock)
    runtime, source, raw = _runtime(tmp_path, provider="taifex", execution_profile=profile)
    feed = runtime.feeds[0]
    feed.update(
        dataset_key="tw_futures_eod",
        market="TW",
        asset_class="future",
        source_timezone="Asia/Taipei",
        scheduled_time="13:45",
        target_date_lag_days=0,
    )
    runtime.installed_feeds = [feed]
    runtime.state.bind_profile(profile)
    runtime._ensure_proof = lambda: None
    source.request = lambda *a, **kw: {
        "acquisition_allowed": True,
        "admitted_dataset_keys": [feed["dataset_key"]],
    }
    source.plans_for_date = lambda **kw: {"data": []}
    source.universes = lambda *a, **kw: {
        "activation": {"enabled": True, "activation_date": str(DAY)},
        "published_release_id": "old",
    }
    source.release = lambda identity: {
        "release_id": identity,
        "provider": "taifex",
        "dataset_key": feed["dataset_key"],
        "effective_date": str(DAY - timedelta(days=1)),
        "status": "published",
    }
    return runtime, source, feed


def plan(profile, day=DAY):
    return {
        "provider": "taifex",
        "execution_profile": profile,
        "dataset_key": "tw_futures_eod",
        "trade_date": str(day),
        "release_id": "dated",
        "plan_id": str(day),
        "status": "complete",
        "parts": [],
        "summary": {
            "expected": 0,
            "data": 0,
            "no_data": 0,
            "missing": 0,
            "blocked": 0,
            "is_late": False,
        },
    }


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
def test_skipped_complete_cycle_never_refreshes_taifex(tmp_path, monkeypatch, profile):
    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    source.plan = lambda **kw: {**plan(profile), "status": "skipped_calendar"}
    runtime.sync_universes = lambda **kw: pytest.fail("refresh before Source skip")
    runtime._snapshot = lambda *a, **kw: pytest.fail("provider request on skipped date")
    assert runtime.state.refresh_due("taifex", 10**12)
    runtime.cycle()
    assert runtime.state.health("taifex")["date_prerequisites"] == []
    runtime.close()


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
def test_prepared_transport_failure_recovers_while_controls_stopped(tmp_path, monkeypatch, profile):
    from types import SimpleNamespace
    from uuid import UUID

    from findb_fetcher.client import SourceAPITransportError

    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    frozen = plan(profile)
    frozen.update(
        status="pending", parts=[{"work_item_id": "fp1:test", "member_keys": ["TX1", "TX2"]}]
    )
    frozen["summary"].update(expected=2, missing=2)
    runtime.state.record_plan(frozen, "taifex")
    source.release = lambda identity: {
        "members": [{"member_key": k, "mapping_status": "mapped"} for k in ["TX1", "TX2"]]
    }
    source.evaluate = lambda identity: frozen
    source.request = lambda *a, **kw: {"acquisition_allowed": False}
    runtime.can_acquire = lambda: False
    runtime._acquire = lambda *a: pytest.fail("stopped acquisition")
    key = "fp1:test:TX1"
    body = b'[{"frozen":"body"}]'
    receipt = str(UUID(int=1))
    runtime.state.prepare(key, body)
    runtime.state.finish(key, status="prepared", receipt=receipt)
    runtime.delivery.prepare = lambda value: value
    runtime.delivery.deliver = lambda *a: pytest.fail("receipt must not be delivered again")
    runtime.delivery.get_run_status = lambda *a, **kw: (_ for _ in ()).throw(
        SourceAPITransportError("offline")
    )
    runtime.drain()
    runtime.cycle(drain_prepared=False)
    assert runtime.state.prepared(key) == body and runtime.state.receipt(key) == receipt
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET next_at=0 WHERE key=?", (key,))
    runtime.delivery.get_run_status = lambda *a, **kw: SimpleNamespace(
        status="completed", failed_records=0
    )
    runtime.drain()
    runtime.cycle(drain_prepared=False)
    with runtime.state.connection() as db:
        statuses = db.execute("SELECT status FROM full_work ORDER BY key").fetchall()
    assert [row[0] for row in statuses] == ["complete", "pending"]
    assert runtime.state.prepared(key) == body and runtime.state.receipt(key) == receipt
    runtime.close()


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("reason", ["dated_report_unavailable", "dated_universe_pending"])
def test_blocked_new_date_preserves_older_frozen_progress_and_restart(
    tmp_path, monkeypatch, profile, reason
):
    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    older = DAY - timedelta(days=1)
    source.universes = lambda *a, **kw: {
        "activation": {"enabled": True, "activation_date": str(older)},
        "published_release_id": "old",
    }
    source.plans_for_date = lambda **kw: {
        "data": [plan(profile, older)] if kw["trade_date"] == str(older) else []
    }
    source.plan = lambda **kw: (_ for _ in ()).throw(
        FullMarketProtocolError("dated required", status_code=409, code=CODE)
    )
    calls = []

    def unavailable(*a):
        calls.append(a)
        raise DatedUniversePendingError(reason)

    runtime._dated_taifex_universe = unavailable
    evaluated = []
    source.evaluate = lambda identity: evaluated.append(identity) or plan(profile, older)
    runtime.cycle()
    assert evaluated == [str(older)] and len(calls) == 1
    state = FullMarketState(runtime.state.path)
    state.bind_profile(profile)
    assert state.health("taifex")["date_prerequisites"][0]["reason"] == reason
    runtime.state = state
    runtime.cycle()
    assert len(calls) == 1 and evaluated == [str(older), str(older)]
    state.clock = lambda: 10**12
    runtime.cycle()
    assert len(calls) == 2
    runtime.close()


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize(
    "error",
    [
        QuotaBlockedError("cooldown"),
        TaifexOperationalError("HTTP429"),
        FullMarketProtocolError("denied", status_code=403),
    ],
)
def test_operational_failure_aborts_cycle_without_date_retry_state(
    tmp_path, monkeypatch, profile, error
):
    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    source.plan = lambda **kw: (_ for _ in ()).throw(
        FullMarketProtocolError("dated required", status_code=409, code=CODE)
    )
    runtime._dated_taifex_universe = lambda *a: (_ for _ in ()).throw(error)
    with pytest.raises(type(error)):
        runtime.cycle()
    assert runtime.state.health("taifex")["date_prerequisites"] == []
    runtime.close()


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
def test_new_exact_date_release_and_concurrent_freeze(tmp_path, monkeypatch, profile):
    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    calls = []

    def create(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise FullMarketProtocolError("dated required", status_code=409, code=CODE)
        return plan(profile)

    source.plan = create
    runtime._dated_taifex_universe = lambda *a: {"release_id": "dated"}
    assert runtime._plan_for_date(feed, DAY) == plan(profile)
    assert [c["release_id"] for c in calls] == ["old", "dated"]
    source.plans_for_date = lambda **kw: {"data": [plan(profile)]}
    runtime._dated_taifex_universe = lambda *a: pytest.fail("frozen plan refresh")
    assert runtime._plan_for_date(feed, DAY) == plan(profile)
    assert len(calls) == 2
    runtime.close()


@pytest.mark.parametrize(
    "body,code",
    [
        ({"detail": {"code": CODE}}, CODE),
        ({"detail": "secret"}, None),
        ({"detail": {"code": CODE, "token": "secret"}}, None),
        ([], None),
    ],
)
def test_protocol_only_recognizes_exact_static_error(body, code):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(409, json=body))
    ) as http:
        source = FullMarketSourceClient(
            FetcherConfig("https://source.example", "offline-key"), client=http
        )
        with pytest.raises(FullMarketProtocolError) as exc:
            source.plan(
                dataset="tw_futures_eod", provider="taifex", trade_date=str(DAY), release_id="old"
            )
        assert exc.value.code == code and "secret" not in str(exc.value)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_report_http_errors_are_operational(status):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=b"error"))
    ) as http:
        with pytest.raises(TaifexOperationalError):
            fetch_report(http, target_date=DAY, latest=False)


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("status", ["published", "candidate"])
def test_dated_historical_report_builds_actual_contracts_and_reuses_cache(
    tmp_path, monkeypatch, profile, status
):
    from urllib.parse import parse_qs

    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    runtime._snapshot = lambda *a, **kw: b"[]"
    runtime.proof = {"max_response_bytes": 100000}
    runtime._observed_response = lambda *a, **kw: None
    requests = []
    authorization = source.authorization
    scopes = []

    def authorize(*args, **kwargs):
        scopes.append(kwargs.get("required_dataset"))
        return authorization(*args, **kwargs)

    source.authorization = authorize

    def response(request):
        query = parse_qs(request.content.decode())
        product = query["commodity_id"][0]
        assert query["queryStartDate"] == query["queryEndDate"] == [DAY.strftime("%Y/%m/%d")]
        requests.append(product)
        header = "Date,Contract,ContractMonth(Week),Open,High,Low,Last,Volume,SettlementPrice,OpenInterest,TradingSession\n"
        rows = "".join(
            f"{DAY:%Y/%m/%d},{product},202608,100,110,90,105,5,105,10,{session}\n"
            for session in ["一般", "盤後"]
        )
        return httpx.Response(200, content=(header + rows).encode("cp950"))

    runtime.http.close()
    runtime.http = httpx.Client(transport=httpx.MockTransport(response))
    submitted = []

    def submit(body):
        submitted.append(body)
        return {**body, "release_id": "dated", "status": status}

    source.submit = submit
    if status == "candidate":
        with pytest.raises(DatedUniversePendingError, match="dated_universe_pending"):
            runtime._dated_taifex_universe(feed, DAY)
    else:
        assert runtime._dated_taifex_universe(feed, DAY)["release_id"] == "dated"
    assert requests == ["TX", "MTX", "TMF", "TE", "TF"]
    assert scopes and set(scopes) == {"tw_futures_eod"}
    assert {m["contract_code"] for m in submitted[0]["members"]} == {
        p + ":202608" for p in requests
    }
    assert all(set(m["sessions"]) == {"regular", "after_hours"} for m in submitted[0]["members"])
    assert submitted[0]["effective_date"] == str(DAY)
    assert submitted[0]["evidence"][0]["url"].startswith("https://")
    assert all(m["raw_evidence_ref"].startswith("r2://") for m in submitted[0]["members"])
    runtime._taifex_report(DAY, feed["dataset_key"])
    assert len(requests) == 5
    runtime.close()


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("body", [b"", b"bad csv", b"\xff"])
def test_malformed_dated_report_never_submits_or_caches(tmp_path, monkeypatch, profile, body):
    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    runtime._snapshot = lambda *a, **kw: b"[]"
    runtime.proof = {"max_response_bytes": 100000}
    runtime._observed_response = lambda *a, **kw: None
    runtime.http.close()
    runtime.http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    source.submit = lambda *a: pytest.fail("invalid report must not publish")
    with pytest.raises(DatedUniversePendingError, match="dated_report_unavailable"):
        runtime._dated_taifex_universe(feed, DAY)
    assert str(DAY) not in runtime.futures
    runtime.close()


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("latest", [b"", b"[]"])
def test_empty_report_real_raw_store_defers_date_and_preserves_catchup(
    tmp_path, monkeypatch, profile, latest
):
    from findb_fetcher.raw_storage import R2RawPayloadStore
    from test_raw_storage import FakeR2, _config

    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    older = DAY - timedelta(days=1)
    source.universes = lambda *a, **kw: {
        "activation": {"enabled": True, "activation_date": str(older)},
        "published_release_id": "old",
    }
    source.plans_for_date = lambda **kw: {
        "data": [plan(profile, older)] if kw["trade_date"] == str(older) else []
    }
    source.plan = lambda **kw: (_ for _ in ()).throw(
        FullMarketProtocolError("dated required", status_code=409, code=CODE)
    )
    source.submit = lambda *a: pytest.fail("empty report cannot publish a universe")
    runtime.proof = {"max_response_bytes": 100000}
    runtime._observed_response = lambda *a, **kw: None
    client = FakeR2()
    runtime.raw_store = R2RawPayloadStore(_config(), client=client)
    requests = []

    def response(request):
        requests.append(request.method)
        return httpx.Response(200, content=latest if request.method == "GET" else b"")

    runtime.http.close()
    runtime.http = httpx.Client(transport=httpx.MockTransport(response))
    evaluated = []
    source.evaluate = lambda identity: evaluated.append(identity) or plan(profile, older)
    now = runtime.state.clock()
    runtime.cycle()
    assert requests == ["GET", "POST"]
    assert evaluated == [str(older)]
    deferred = runtime.state.health("taifex")["date_prerequisites"]
    assert len(deferred) == 1 and deferred[0]["reason"] == "dated_report_unavailable"
    with runtime.state.connection() as db:
        next_at = db.execute("SELECT next_at FROM full_date_prerequisite").fetchone()[0]
    assert next_at >= now + 3600
    assert str(DAY) not in runtime.futures
    assert all(call["Body"] for call in client.calls)
    runtime.cycle()
    assert requests == ["GET", "POST"] and evaluated == [str(older), str(older)]
    runtime.close()


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
def test_r2_failure_is_operational_and_does_not_skip_older_work(tmp_path, monkeypatch, profile):
    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    runtime._snapshot = lambda *a, **kw: b"[]"
    runtime.raw_store.persist = lambda *a, **kw: (_ for _ in ()).throw(OSError("R2 unavailable"))
    source.plan = lambda **kw: (_ for _ in ()).throw(
        FullMarketProtocolError("dated required", status_code=409, code=CODE)
    )
    with pytest.raises(OSError, match="R2 unavailable"):
        runtime.cycle()
    assert runtime.state.health("taifex")["date_prerequisites"] == []
    runtime.close()


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
def test_concurrent_plan_after_dated_refresh_returns_frozen_members(tmp_path, monkeypatch, profile):
    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    reads = []

    def lookup(**kw):
        reads.append(kw)
        return {"data": [plan(profile)] if len(reads) == 3 else []}

    source.plans_for_date = lookup
    source.plan = lambda **kw: (_ for _ in ()).throw(
        FullMarketProtocolError("conflict", status_code=409, code=CODE)
    )
    runtime._dated_taifex_universe = lambda *a: {"release_id": "dated"}
    assert runtime._plan_for_date(feed, DAY) == plan(profile)
    assert len(reads) == 3
    runtime.close()


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize(
    "failure",
    [
        "http",
        "transport",
        "authorization",
        "r2",
        "r2_identity",
        "source_delivery",
        "source_transport",
    ],
)
@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("external_drain", [False, True])
def test_frozen_plan_operational_failure_stops_cycle_and_preserves_work(
    tmp_path, monkeypatch, profile, failure, prepared, external_drain
):
    from findb_fetcher.client import SourceAPIResponseError, SourceAPITransportError
    from findb_fetcher.raw_storage import RawStorageUploadError

    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    frozen = plan(profile)
    frozen.update(
        status="pending", parts=[{"work_item_id": "fp1:test", "member_keys": ["TX1", "TX2"]}]
    )
    frozen["summary"].update(expected=2, missing=2)
    runtime.state.record_plan(frozen, "taifex")
    source.plans_for_date = lambda **kw: {"data": [frozen]}
    original_release = source.release
    source.release = lambda identity: {
        **original_release(identity),
        "members": [{"member_key": k, "mapping_status": "mapped"} for k in ["TX1", "TX2"]],
    }
    source.evaluate = lambda identity: frozen
    errors = {
        "http": TaifexOperationalError("HTTP503"),
        "transport": httpx.ConnectError("offline"),
        "authorization": FullMarketProtocolError("denied", status_code=403),
        "r2": RawStorageUploadError("R2 unavailable"),
        "r2_identity": runtime_module.ReadinessBlockedError("R2 identity mismatch"),
        "source_delivery": SourceAPIResponseError(503),
        "source_transport": SourceAPITransportError("offline"),
    }
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise errors[failure]

    body = b'[{"frozen":"body"}]'
    if prepared:
        runtime.state.prepare("fp1:test:TX1", body)
        runtime.delivery.prepare = fail
        runtime._acquire = lambda *a: pytest.fail("prepared work must not reacquire")
    else:
        runtime._taifex_report = fail
    if external_drain:
        runtime.drain()
    runtime.cycle(drain_prepared=not external_drain)
    assert len(calls) == 1
    with runtime.state.connection() as db:
        rows = db.execute(
            "SELECT status,attempts,lease_until FROM full_work ORDER BY key"
        ).fetchall()
    assert tuple(rows[0]) == ("prepared" if prepared else "pending", 1, 0)
    assert tuple(rows[1]) == ("pending", 0, 0)
    assert runtime.state.prepared("fp1:test:TX1") == (body if prepared else None)
    assert runtime.state.health("taifex")["date_prerequisites"] == []
    runtime.close()
