"""Source-first dated TAIFEX prerequisites preserve frozen work and quotas."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

import findb_fetcher.full_market_runtime as runtime_module
from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.full_market_runtime import DatedUniversePendingError
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
def test_queued_dated_prerequisite_uses_prerequisite_retry_delay(tmp_path, monkeypatch, profile):
    from findb_fetcher import provider_work

    runtime, _, feed = setup_runtime(tmp_path, monkeypatch, profile)
    owner = SimpleNamespace(
        work={
            "kind": "daily",
            "dataset_key": feed["dataset_key"],
            "state": "open",
            "trade_date": str(DAY),
            "scope": {"feed": feed},
        }
    )

    @contextmanager
    def claimed(*args, **kwargs):
        yield owner

    monkeypatch.setattr(provider_work, "claimed_work", claimed)
    runtime._plan_for_date = lambda *args: (_ for _ in ()).throw(
        DatedUniversePendingError("dated_universe_pending", retry_after_seconds=3599)
    )
    settlements = []
    runtime._settle_queued_round = lambda *args, **kwargs: settlements.append(kwargs)

    runtime._queued_cycle()

    assert settlements == [
        {
            "outcome": "retry",
            "failure_code": "DATED_UNIVERSE_PENDING",
            "retry_delay_seconds": 3599,
        }
    ]
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
    assert runtime.raw_store.formats.count("csv_cp950") == 5
    assert runtime.raw_store.formats[-1] == "json"
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


# Official futDataDown layout (2026-10-29/30): 19 headers, rows end with a trailing comma.
OFFICIAL_HEADER = (
    "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,"
    "未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,"
    "交易時段,價差對單式委託成交量\n"
)


def official_report(product, *, sessions=("一般", "盤後")):
    rows = {
        "一般": f"{DAY:%Y/%m/%d},{product},202608  ,47187,47218,46310,46577,-672,-1.42%,"
        "56666,46569,117523,46579,46584,50440,44435,,一般,,\n",
        "盤後": f"{DAY:%Y/%m/%d},{product},202608  ,47280,47607,46968,47121,-128,-0.27%,"
        "31298,-,-,47108,47127,50440,44435,,盤後,,\n",
    }
    spread = (
        f"{DAY:%Y/%m/%d},{product},202608/202609    ,127,127,119,119,-,-,283,-,-,"
        "119,122,170,119,,一般,296,\n"
    )
    body = OFFICIAL_HEADER + "".join(rows[session] for session in sessions)
    return (body + (spread if sessions else "")).encode("cp950")


def test_official_report_layout_maps_sessions_and_skips_spreads():
    from findb_fetcher.providers.taifex import parse_report

    rows = parse_report(official_report("TX"), target_date=DAY, historical=True)
    assert [(row["contract_code"], row["session"]) for row in rows] == [
        ("TX:202608", "after_hours"),
        ("TX:202608", "regular"),
    ]
    after_hours, regular = rows
    assert regular["settlement_price"] == "46569" and regular["open_interest"] == 117523
    assert after_hours["settlement_price"] is None and after_hours["open_interest"] is None


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("sessions", [(), ("盤後",)], ids=["closure", "after_hours_only"])
def test_unpublished_official_report_stays_pending_and_uncached(
    tmp_path, monkeypatch, profile, sessions
):
    """Closures return a header-only file; the regular session is published after 05:00 rows."""
    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, profile)
    runtime._snapshot = lambda *a, **kw: b"[]"
    runtime.proof = {"max_response_bytes": 100000}
    runtime._observed_response = lambda *a, **kw: None
    runtime.http.close()
    runtime.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=official_report(
                    parse_qs(request.content.decode())["commodity_id"][0], sessions=sessions
                ),
            )
        )
    )
    source.submit = lambda *a: pytest.fail("unpublished report must not publish membership")
    with pytest.raises(DatedUniversePendingError, match="dated_report_unavailable"):
        runtime._dated_taifex_universe(feed, DAY)
    assert str(DAY) not in runtime.futures
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
