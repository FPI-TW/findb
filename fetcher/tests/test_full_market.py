from __future__ import annotations

import io
import json
import stat
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_cli import load_config
from findb_fetcher.full_market_client import FullMarketSourceClient
from findb_fetcher.full_market_runtime import (
    ReadinessBlockedError,
    load_readiness,
    split_rows,
    validate_capacity,
)
from findb_fetcher.full_market_state import FullMarketState, FullMarketStateError, QuotaBlockedError
from findb_fetcher.full_market_universe import (
    UniverseSnapshotError,
    canonical_bytes,
    map_catalogue,
    parse_hkex,
    parse_nasdaq,
    parse_tw_companies,
    parse_tw_etfs,
    universe_request,
)
from findb_fetcher.providers.finlab import FinLabDatasetConfig, FinLabSymbol
from findb_fetcher.providers.shioaji_session import ShioajiSession
from findb_fetcher.providers.taifex import fetch_report, parse_report, report_members
from findb_fetcher.readiness_probe import probe_twelve_data


def test_nasdaq_major_common_adr_excludes_other_instruments() -> None:
    raw = b"Symbol|Security Name|Test Issue|ETF\nAAPL|Apple Common Stock|N|N\nADR|Company American Depositary Shares|N|N\nQQQ|Fund Common Stock|N|Y\nPREF|Company Preferred Stock|N|N\nWRT|Company Warrants|N|N\nUNIT|Company Units|N|N\nTEST|Test Common Stock|Y|N\nODD|Unclassified Security|N|N\nFile Creation Time: 10012026||\n"
    members = parse_nasdaq(raw)
    assert [item["symbol"] for item in members] == ["AAPL", "ADR", "ODD"]
    assert members[1]["classification"] == "adr"
    assert members[2]["classification"] == "classification_gap"
    other = b"ACT Symbol|Security Name|Test Issue|ETF|Exchange\nBRK.B|Class B Common Stock|N|N|N\nOTC|Common Stock|N|N|O\n"
    assert parse_nasdaq(other, other=True)[0]["symbol"] == "BRK.B"
    assert len(parse_nasdaq(other, other=True)) == 1


def _hk_workbook(day: str) -> bytes:
    rows = [f'<row><c r="A2" t="inlineStr"><is><t>Updated as at{day}</t></is></c></row>']
    for number, code, currency, category, board in (
        (4, "700", "HKD", "Equity", "Main Board"),
        (5, "83000", "CNY", "Equity", "GEM"),
        (6, "1", "USD", "Exchange Traded Products", "Main Board"),
    ):
        cells = {"A": code, "B": "Company", "C": category, "D": board, "Q": currency}
        rows.append(
            "<row>"
            + "".join(
                f'<c r="{column}{number}" t="inlineStr"><is><t>{value}</t></is></c>'
                for column, value in cells.items()
            )
            + "</row>"
        )
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        + "".join(rows)
        + "</sheetData></worksheet>"
    )
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return data.getvalue()


def test_hkex_codes_board_currency_and_future_snapshot() -> None:
    raw = _hk_workbook("01/10/2026")
    effective, members = parse_hkex(raw, observed_date=date(2026, 10, 1))
    assert effective == date(2026, 10, 1)
    assert [(member["symbol"], member["currency"]) for member in members] == [
        ("00700", "HKD"),
        ("83000", "CNY"),
    ]
    with pytest.raises(UniverseSnapshotError, match="future-dated"):
        parse_hkex(_hk_workbook("02/10/2026"), observed_date=date(2026, 10, 1))
    mapped = map_catalogue(
        members, [{"symbol": "700", "mic_code": "XHKG", "currency": "HKD", "type": "Common Stock"}]
    )
    assert mapped[0]["symbol"] == "00700"
    assert mapped[0]["provider_symbol"] == "700"
    assert mapped[1]["mapping_status"] == "gap"


def test_tw_etf_sections_include_active_leveraged_inverse_bond_only() -> None:
    html = "<table><tr><td>股票</td></tr><tr><td>2330 台積電</td><td>x</td><td>x</td><td>x</td><td>x</td><td>x</td></tr><tr><td>ETF</td></tr>"
    for symbol in ("00980A", "00631L", "00632R", "00679B"):
        html += f"<tr><td>{symbol}　ETF</td><td>x</td><td>x</td><td>x</td><td>x</td><td>x</td></tr>"
    html += "<tr><td>ETN</td></tr><tr><td>020000 ETN</td><td>x</td><td>x</td><td>x</td><td>x</td><td>x</td></tr></table>"
    members = parse_tw_etfs(html.encode("cp950"), otc=True)
    assert [member["symbol"] for member in members] == ["00980A", "00631L", "00632R", "00679B"]
    assert all(
        member["exchange"] == "ROCO" and member["asset_class"] == "etf" for member in members
    )
    assert parse_tw_companies('[{"公司代號":"2330"}]'.encode())[0]["classification"] == "ordinary"


def test_symbol_mapping_never_drops_gap_or_guesses_exchange() -> None:
    members = [
        {
            "symbol": "BRK.B",
            "exchange": "XNYS",
            "currency": "USD",
            "classification": "ordinary",
            "mapping_status": "gap",
            "provider_symbol": "BRK.B",
        }
    ]
    assert (
        map_catalogue(
            members,
            [{"symbol": "BRK-B", "mic_code": "XNYS", "currency": "USD", "type": "Common Stock"}],
        )[0]["mapping_status"]
        == "gap"
    )
    assert (
        map_catalogue(
            members,
            [{"symbol": "BRK.B", "mic_code": "XNAS", "currency": "USD", "type": "Common Stock"}],
        )[0]["mapping_status"]
        == "gap"
    )
    assert (
        map_catalogue(
            members,
            [{"symbol": "BRK.B", "mic_code": "XNYS", "currency": "USD", "type": "Common Stock"}],
        )[0]["mapping_status"]
        == "mapped"
    )


def _taifex(product: str = "TX", month: str = "202610W1", session: str = "盤後") -> dict[str, str]:
    return {
        "Date": "20261001",
        "Contract": product,
        "ContractMonth(Week)": month,
        "Open": "100",
        "High": "110",
        "Low": "90",
        "Last": "105",
        "Volume": "37",
        "SettlementPrice": "-",
        "OpenInterest": "-",
        "TradingSession": session,
    }


def test_taifex_actual_week_month_sessions_and_no_synthetic_volume() -> None:
    rows = parse_report(
        canonical_bytes(
            [_taifex(), _taifex(month="202610", session="一般"), _taifex(month="202610/202611")]
        ),
        target_date=date(2026, 10, 1),
    )
    assert len(rows) == 2
    assert all(
        row["volume"] == 37 and row["open_interest"] is None and row["settlement_price"] is None
        for row in rows
    )
    assert {row["session"] for row in rows} == {"regular", "after_hours"}
    assert {member["contract_code"] for member in report_members(rows)} == {
        "TX:202610",
        "TX:202610W1",
    }
    assert all("expiry_date" not in member for member in report_members(rows))
    with pytest.raises(UniverseSnapshotError, match="attribution"):
        parse_report(canonical_bytes([_taifex()]), target_date=date(2026, 9, 30))
    with pytest.raises(UniverseSnapshotError, match="duplicated"):
        parse_report(canonical_bytes([_taifex(), _taifex()]), target_date=date(2026, 10, 1))
    with pytest.raises(UniverseSnapshotError, match="empty"):
        parse_report(b"[]", target_date=date(2026, 10, 1))


def test_taifex_history_requests_exact_product_and_date() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.content.decode())
        return httpx.Response(200, content=b"raw")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        fetch_report(client, target_date=date(2026, 10, 1), latest=False, product="TMF")
    assert "commodity_id=TMF" in calls[0]
    assert "queryStartDate=2026%2F10%2F01" in calls[0]


def test_full_scale_rows_bound_bytes_and_row_count_without_loss() -> None:
    rows = [
        {"symbol": f"S{index:05}", "close": "1234567890.123456", "name": "台灣股票" * 20}
        for index in range(30000)
    ]
    chunks = split_rows(rows, max_rows=1000, max_bytes=50000)
    assert [row for chunk in chunks for row in chunk] == rows
    assert all(len(chunk) <= 1000 and len(canonical_bytes(chunk)) <= 50000 for chunk in chunks)
    with pytest.raises(ReadinessBlockedError, match="one provider row"):
        split_rows([{"x": "a" * 100}], max_bytes=10)


def test_quota_shared_us_hk_persists_restart_and_429(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    first = FullMarketState(path)
    kwargs = {
        "account": "twelve_data",
        "window": "2026-10-01",
        "requests": 1,
        "byte_count": 50,
        "request_limit": 2,
        "byte_limit": 100,
        "now": 1.0,
    }
    assert first.reserve(**kwargs) == (0, 1)
    second = FullMarketState(path)
    assert second.reserve(**kwargs) == (1, 2)
    with pytest.raises(QuotaBlockedError):
        first.reserve(**kwargs)
    first.rate_limited(account="twelve_data", window="2026-10-01", until=10)
    with pytest.raises(QuotaBlockedError):
        second.reserve(**{**kwargs, "request_limit": 100, "byte_limit": 10000, "now": 2})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def _plan() -> dict[str, Any]:
    return {
        "plan_id": "p",
        "dataset_key": "us_equity_eod",
        "trade_date": "2026-10-01",
        "parts": [{"work_item_id": "fp1:123", "member_keys": ["AAPL", "MSFT"]}],
        "summary": {
            "expected": 2,
            "data": 0,
            "no_data": 0,
            "missing": 2,
            "blocked": 0,
            "is_late": False,
        },
    }


def test_durable_prepared_body_lease_restart_terminal_manual_and_late(tmp_path: Path) -> None:
    state = FullMarketState(tmp_path / "state.sqlite3")
    plan = _plan()
    state.record_plan(plan, "twelve_data")
    key = "fp1:123:AAPL"
    assert state.claim(key, now=1, lease_seconds=2)
    assert not state.claim(key, now=2)
    body = canonical_bytes([{"fetched_at": "stable", "source_raw_sha256": "a" * 64}])
    state.prepare(key, body)
    restarted = FullMarketState(state.path)
    assert restarted.claim(key, now=4)
    assert restarted.prepared(key) == body
    with pytest.raises(FullMarketStateError, match="cannot be changed"):
        restarted.prepare(key, b"changed")
    restarted.finish(key, status="manual", reason="mapping_gap")
    assert not restarted.claim(key, now=10000)
    plan["summary"]["is_late"] = True
    restarted.evaluate(plan)
    assert restarted.health("twelve_data")["manual_required"] == 1
    assert restarted.health("twelve_data")["unresolved"][0]["late"] == 1
    plan["summary"].update(data=1, no_data=1, missing=0, blocked=0)
    restarted.evaluate(plan)
    assert restarted.health("twelve_data")["unresolved"] == []


def test_catchup_never_pre_activation_includes_all_actual_open_dates(tmp_path: Path) -> None:
    state = FullMarketState(tmp_path / "state.sqlite3")
    dates = [date(2026, 9, 29), date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5)]
    assert (
        state.catchup_dates(
            "twelve_data",
            "us_equity_eod",
            activation=date(2026, 10, 1),
            through=date(2026, 10, 2),
            open_dates=dates,
        )
        == dates[1:3]
    )
    state.record_plan(_plan(), "twelve_data")
    state.record_plan(_plan(), "twelve_data")
    assert len(state.pending_plans("twelve_data")) == 1


def _proof(now: datetime) -> dict[str, Any]:
    return {
        "provider": "twelve_data",
        "status": "verified",
        "datasets": ["us_equity_eod", "hk_equity_eod"],
        "verified_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "evidence_url": "https://provider.example/usage",
        "evidence_sha256": "a" * 64,
        "requests_per_day": 10000,
        "requests_per_minute": 2000,
        "bytes_per_day": 100000000,
        "max_response_bytes": 1000,
        "requests_per_second": 10,
    }


def test_unknown_or_insufficient_readiness_blocks_no_false_subset(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ReadinessBlockedError, match="unknown"):
        load_readiness(None, "twelve_data", ["us_equity_eod", "hk_equity_eod"], now=now)
    path = tmp_path / "proof.json"
    proof = _proof(now)
    path.write_text(json.dumps(proof))
    assert load_readiness(path, "twelve_data", proof["datasets"], now=now) == proof
    with pytest.raises(ReadinessBlockedError):
        load_readiness(path, "twelve_data", ["us_equity_eod"], now=now)
    validate_capacity(proof, 5000, seconds_available=1000)
    with pytest.raises(ReadinessBlockedError, match="full coverage"):
        validate_capacity(proof, 10000, seconds_available=1000)
    with pytest.raises(ReadinessBlockedError):
        validate_capacity(proof, 5000, seconds_available=10)


def test_sdk_session_one_login_catalogue_tse_otc_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.7.1")
    calls: list[str] = []

    class API:
        def __init__(self, **kwargs: Any) -> None:
            self.Contracts = SimpleNamespace(
                Stocks=SimpleNamespace(
                    TSE=[SimpleNamespace(code="2330", exchange="TSE")],
                    OTC=[SimpleNamespace(code="6488", exchange="OTC")],
                )
            )

        def login(self, **kwargs: Any) -> None:
            calls.append("login")

        def logout(self) -> None:
            calls.append("logout")

        def usage(self) -> Any:
            return SimpleNamespace(bytes=10)

        def kbars(self, contract: Any, **kwargs: Any) -> dict[str, list[Any]]:
            calls.append(contract.code)
            return {
                "ts": [],
                "Open": [],
                "High": [],
                "Low": [],
                "Close": [],
                "Volume": [],
                "Amount": [],
            }

    session = ShioajiSession(
        "secret", "other-secret", simulation=False, sdk=SimpleNamespace(Shioaji=API)
    )
    assert len(session.catalogue()) == 2
    session.fetch_kbars("2330", date(2026, 10, 1))
    session.fetch_kbars("6488", date(2026, 10, 1))
    session.close()
    assert calls == ["login", "2330", "6488", "logout"]


def test_finlab_etf_only_explicit_production_scope() -> None:
    config = FinLabDatasetConfig(
        dataset_key="tw_etf_eod",
        market="TW",
        asset_class="etf",
        currency="TWD",
        field_datasets={
            "open": "price:開盤價",
            "high": "price:最高價",
            "low": "price:最低價",
            "close": "price:收盤價",
            "volume": "price:成交股數",
        },
        symbols=(FinLabSymbol("00980A", "00980A"),),
        production_scope=True,
    )
    assert config.asset_class == "etf"


def test_manifest_stopped_seven_feeds_no_staging_changes() -> None:
    config = load_config(
        Path(__file__).resolve().parents[1] / "configs/full_market.production.v1.json"
    )
    assert config["desired_state"] == "stopped"
    assert len(config["feeds"]) == 7


def test_universe_version_evidence_checksum_stable_members() -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    request = universe_request(
        dataset_key="us_equity_eod",
        provider="twelve_data",
        effective_date=now.date(),
        observed_at=now,
        source_timezone="America/New_York",
        snapshots={"https://official.example/list": b"abc"},
        members=[{"symbol": "B"}, {"symbol": "A"}],
    )
    assert request["version"] == 1
    assert (
        request["evidence"][0]["sha256"]
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert request["members"][0]["symbol"] == "A"


def test_source_protocol_exact_server_owned_plan_no_deadline_or_size() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"plan_id": "p"})

    config = FetcherConfig("https://source.example", "source-secret")
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        source = FullMarketSourceClient(config, client=http)
        source.plan(
            dataset="us_equity_eod", provider="twelve_data", trade_date="2026-10-01", release_id="r"
        )
    assert set(json.loads(calls[0].content)) == {
        "version",
        "dataset_key",
        "provider",
        "trade_date",
        "release_id",
    }
    assert calls[0].headers["X-API-Key"] == "source-secret"


def test_live_readiness_probe_stops_after_one_credit_if_insufficient() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert "secret" not in str(request.url)
        if request.url.path == "/api_usage":
            return httpx.Response(
                200,
                json={"plan_limit": 8, "current_usage": 1, "plan_daily_limit": 1, "daily_usage": 0},
            )
        if "nasdaqlisted" in request.url.path:
            return httpx.Response(
                200, content=b"Symbol|Security Name|Test Issue|ETF\nAAPL|Common Stock|N|N\n"
            )
        return httpx.Response(
            200,
            content=b"ACT Symbol|Security Name|Test Issue|ETF|Exchange\nIBM|Common Stock|N|N|N\n",
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = probe_twelve_data("secret", client=client)
    assert result["status"] == "blocked"
    assert result["requests_made"] == 1
    assert len(calls) == 3  # One provider account call, two public official snapshots.
    assert "secret" not in json.dumps(result)


def _runtime(tmp_path: Path, *, provider: str = "twelve_data") -> tuple[Any, Any, Any]:
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.raw_storage import RawObject

    state = FullMarketState(tmp_path / "state.sqlite3")

    class Source:
        def __init__(self) -> None:
            self.outcomes: list[Any] = []

        def outcome(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            self.outcomes.append(kwargs)
            return {}

    class Raw:
        def __init__(self) -> None:
            self.calls: list[bytes] = []

        def persist(self, raw: bytes, **kwargs: Any) -> Any:
            import hashlib

            self.calls.append(raw)
            return RawObject(
                "r2://" + "a" * 32 + "/raw-test/raw.json", hashlib.sha256(raw).hexdigest(), len(raw)
            )

    source, raw = Source(), Raw()
    config = {
        "feeds": [
            {
                "provider": provider,
                "dataset_key": "us_equity_eod",
                "market": "US",
                "asset_class": "equity",
                "slot_id": "western_markets_window",
            }
        ]
    }
    runtime = FullMarketRuntime(
        provider=provider,
        config=config,
        state=state,
        source=source,
        delivery=SimpleNamespace(),
        calendar=SimpleNamespace(),
        raw_store=raw,
        readiness_file=None,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )
    runtime.proof = _proof(datetime.now(timezone.utc))
    return runtime, source, raw


def _member() -> dict[str, Any]:
    return {
        "symbol": "AAPL",
        "provider_symbol": "AAPL",
        "exchange": "XNAS",
        "currency": "USD",
        "mapping_status": "mapped",
        "member_key": "AAPL",
    }


def test_raw_persisted_before_provider_mapping_failure(tmp_path: Path) -> None:
    from findb_fetcher.providers.twelve_data import TwelveDataResponse

    runtime, _, raw = _runtime(tmp_path)
    runtime.td = SimpleNamespace(
        fetch_daily=lambda *args, **kwargs: TwelveDataResponse(
            {"status": "ok", "meta": {}}, raw_bytes=b'{"meta":{}}'
        )
    )
    plan = _plan()
    part = plan["parts"][0]
    with pytest.raises(Exception):
        runtime._acquire(_member(), plan, part, {"release_id": "r"}, runtime.feeds[0])
    assert raw.calls == [b'{"meta":{}}']
    runtime.http.close()


def test_provider_not_recalled_after_prepared_body_and_receipt_restart(tmp_path: Path) -> None:
    from uuid import UUID

    runtime, source, _ = _runtime(tmp_path)
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL"]
    runtime.state.record_plan(plan, "twelve_data")
    key = "fp1:123:AAPL"
    request = {"identity": "stable", "fetched_at": "persisted"}
    runtime.state.prepare(key, canonical_bytes([request]))
    counts = {"delivered": 0, "status": 0}

    class Delivery:
        def prepare(self, value: Any) -> Any:
            assert value == request
            return value

        def deliver(self, value: Any) -> Any:
            counts["delivered"] += 1
            return SimpleNamespace(run_id=UUID(int=1))

        def get_run_status(self, *args: Any, **kwargs: Any) -> Any:
            counts["status"] += 1
            return SimpleNamespace(
                status="processing" if counts["status"] == 1 else "completed", failed_records=0
            )

    runtime.delivery = Delivery()
    runtime._acquire = lambda *args: pytest.fail("provider must not be recalled")
    release = {"members": [_member()]}
    runtime._deliver_plan(plan, release, runtime.feeds[0])
    assert source.outcomes == []
    assert counts == {"delivered": 1, "status": 1}
    runtime.state = FullMarketState(runtime.state.path)
    # Advance past retry delay without waiting.
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET next_at=0")
    runtime._deliver_plan(plan, release, runtime.feeds[0])
    assert counts == {"delivered": 1, "status": 2}
    assert runtime.state.receipt(key) == str(UUID(int=1))
    runtime.http.close()


def test_mapping_gap_does_not_become_no_data_or_call_provider(tmp_path: Path) -> None:
    runtime, source, _ = _runtime(tmp_path)
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL"]
    runtime.state.record_plan(plan, "twelve_data")
    runtime._acquire = lambda *args: pytest.fail("gap must not call provider")
    runtime._deliver_plan(
        plan, {"members": [{**_member(), "mapping_status": "gap"}]}, runtime.feeds[0]
    )
    assert source.outcomes[0]["reason"] == "mapping_gap"
    assert runtime.state.health("twelve_data")["manual_required"] == 1
    runtime.http.close()


def test_empty_provider_values_are_blocked_source_error_not_no_trade(tmp_path: Path) -> None:
    from findb_fetcher.providers.twelve_data import TwelveDataResponse

    runtime, source, _ = _runtime(tmp_path)
    runtime.td = SimpleNamespace(
        fetch_daily=lambda *args, **kwargs: TwelveDataResponse(
            {
                "status": "ok",
                "meta": {
                    "interval": "1day",
                    "symbol": "AAPL",
                    "type": "Common Stock",
                    "currency": "USD",
                    "exchange": "NASDAQ",
                    "mic_code": "XNAS",
                },
                "values": [],
            },
            raw_bytes=b'{"values":[]}',
        )
    )
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL"]
    runtime.state.record_plan(plan, "twelve_data")
    runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
    assert source.outcomes[0]["reason"] == "source_error"
    assert runtime.state.health("twelve_data")["unresolved"]
    runtime.http.close()


def test_live_official_shape_regressions_tdr_tpex_english_and_taifex_null() -> None:
    assert [
        item["symbol"]
        for item in parse_tw_companies('[{"公司代號":"1101"},{"公司代號":"910322"}]'.encode())
    ] == ["1101"]
    assert (
        parse_tw_companies(b'[{"SecuritiesCompanyCode":"6488"}]', otc=True)[0]["exchange"] == "ROCO"
    )
    row = {**_taifex(), "SettlementPrice": "NULL", "OpenInterest": "NULL"}
    parsed = parse_report(canonical_bytes([row]), target_date=date(2026, 10, 1))[0]
    assert parsed["settlement_price"] is None and parsed["open_interest"] is None


@pytest.mark.parametrize("product", ["TX", "MTX", "TMF", "TE", "TF"])
@pytest.mark.parametrize("session", ["一般", "盤後"])
@pytest.mark.parametrize(
    "observation",
    [
        {"SettlementPrice": "104.5", "OpenInterest": "0"},
        {"Open": "100", "High": "105"},
        {"Volume": "1"},
    ],
)
def test_taifex_acquire_prepares_provided_metrics_with_null_close(
    tmp_path: Path, contracts_dir: Path, product: str, session: str, observation: dict[str, str]
) -> None:
    runtime, source, raw = _runtime(tmp_path, provider="taifex")
    day = date(2026, 10, 1)
    source_row = {
        **_taifex(product=product, month="202610", session=session),
        "Open": "NULL",
        "High": "NULL",
        "Low": "NULL",
        "Last": "NULL",
        "Volume": "0",
        "SettlementPrice": "NULL",
        "OpenInterest": "NULL",
        **observation,
    }
    report = canonical_bytes([source_row])
    runtime.http.close()
    runtime.http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=report))
    )
    runtime.proof["max_response_bytes"] = 10000
    parsed = parse_report(report, target_date=day)[0]
    member = {
        **report_members([parsed])[0],
        "session": parsed["session"],
        "member_key": f"{parsed['contract_code']}:{parsed['session']}",
    }
    plan = _plan()
    plan["dataset_key"] = "tw_futures_eod"
    plan["parts"][0] = {
        "work_item_id": "fp1:" + "a" * 32,
        "member_keys": [member["member_key"]],
    }
    feed = {**runtime.feeds[0], "dataset_key": "tw_futures_eod", "slot_id": "taiwan_market_window"}
    runtime.state.record_plan(plan, runtime.provider)
    key = plan["parts"][0]["work_item_id"] + ":" + member["member_key"]
    actual_delivery = SourceAPIClient(
        FetcherConfig("https://source.example", "source-secret"),
        ContractRegistry(contracts_dir),
        client=runtime.http,
    )
    prepared_requests = []

    def prepare(request: Any) -> Any:
        # R2 persistence precedes both the durable preparation and contract validation.
        assert raw.calls and raw.calls[0] == report
        assert runtime.state.prepared(key) is not None
        prepared = actual_delivery.prepare(request)
        prepared_requests.append(json.loads(prepared.body))
        return prepared

    runtime.delivery = SimpleNamespace(
        prepare=prepare,
        deliver=lambda prepared: SimpleNamespace(run_id=UUID(int=1)),
        get_run_status=lambda *args, **kwargs: SimpleNamespace(status="queued", failed_records=0),
    )
    assert runtime._deliver_plan(plan, {"release_id": "r", "members": [member]}, feed)
    assert source.outcomes == [] and len(prepared_requests) == 1
    assert prepared_requests[0]["payload"]["data"] == [parsed]
    assert prepared_requests[0]["payload"]["data"][0]["close"] is None
    assert json.loads(runtime.state.prepared(key))[0] == prepared_requests[0]
    assert runtime.state.receipt(key) == str(UUID(int=1))
    runtime.http.close()


@pytest.mark.parametrize("volume", ["0", "NULL"])
def test_taifex_all_missing_metrics_require_explicit_zero_volume_for_no_trade(
    tmp_path: Path, volume: str
) -> None:
    runtime, source, raw = _runtime(tmp_path, provider="taifex")
    source_row = {
        **_taifex(month="202610", session="一般"),
        **{
            field: "NULL"
            for field in ("Open", "High", "Low", "Last", "SettlementPrice", "OpenInterest")
        },
        "Volume": volume,
    }
    report = canonical_bytes([source_row])
    parsed = parse_report(report, target_date=date(2026, 10, 1))[0]
    # Pin an already acquired report; no live provider operation is needed.
    runtime.futures["2026-10-01"] = (report, [parsed], False)
    member = {
        **report_members([parsed])[0],
        "session": "regular",
        "member_key": "TX:202610:regular",
    }
    plan = {**_plan(), "dataset_key": "tw_futures_eod"}
    if volume == "0":
        assert runtime._acquire(member, plan, plan["parts"][0], {}, runtime.feeds[0]) == []
        assert source.outcomes[0]["reason"] == "no_trade"
        assert json.loads(source.outcomes[0]["evidence"]["source_excerpt"])["volume"] == 0
    else:
        with pytest.raises(ReadinessBlockedError, match="no source-provided observation"):
            runtime._acquire(member, plan, plan["parts"][0], {}, runtime.feeds[0])
        assert source.outcomes == []
    assert raw.calls and runtime.state.prepared("fp1:123:TX:202610:regular") is None
    runtime.http.close()


def test_unknown_hk_equity_subcategory_is_retained_classification_gap() -> None:
    raw = _hk_workbook("01/10/2026")
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(buffer, "w") as target:
        target.writestr(
            "xl/worksheets/sheet1.xml",
            source.read("xl/worksheets/sheet1.xml").replace(b"Main Board", b"Investment Companies"),
        )
    _, members = parse_hkex(buffer.getvalue(), observed_date=date(2026, 10, 1))
    assert members[0]["symbol"] == "00700" and members[0]["classification"] == "classification_gap"


def test_universe_refresh_state_backoff_and_health_restart(tmp_path: Path) -> None:
    state = FullMarketState(tmp_path / "state.sqlite3")
    assert state.refresh_due("twelve_data", 1)
    state.refresh("twelve_data", now=1, result=[{"status": "blocked"}])
    restarted = FullMarketState(state.path)
    assert not restarted.refresh_due("twelve_data", 1000)
    assert restarted.refresh_due("twelve_data", 4000)
    assert restarted.health("twelve_data")["universe_refresh"] == [{"status": "blocked"}]


def test_taifex_actual_friday_week_is_preserved_without_broker_aliases() -> None:
    rows = parse_report(
        canonical_bytes([_taifex("MTX", "202610F1")]), target_date=date(2026, 10, 1)
    )
    assert rows[0]["contract_code"] == "MTX:202610F1"
    assert report_members(rows)[0]["classification"] == "weekly"


def test_nasdaq_effective_timestamp_uses_official_date_not_utc_today() -> None:
    from findb_fetcher.full_market_universe import nasdaq_effective_date

    assert nasdaq_effective_date(
        b"File Creation Time: 0930202621:31||||", observed_date=date(2026, 9, 30)
    ) == date(2026, 9, 30)
    with pytest.raises(UniverseSnapshotError, match="future-dated"):
        nasdaq_effective_date(
            b"File Creation Time: 1002202621:31||||", observed_date=date(2026, 10, 1)
        )


def test_full_minute_request_retains_canonical_sequence_identity(
    tmp_path: Path, contracts_dir: Path
) -> None:
    from findb_fetcher.contracts import ContractRegistry
    from findb_fetcher.providers.shioaji import ShioajiKbarsSnapshot

    runtime, _, _ = _runtime(tmp_path, provider="shioaji")
    member = {**_member(), "symbol": "2330", "provider_symbol": "2330", "member_key": "2330"}
    day = date(2026, 10, 1)
    end = datetime(2026, 10, 1, 9, 1)
    nanos = int((end - datetime(1970, 1, 1)).total_seconds()) * 1000000000
    runtime.sdk = SimpleNamespace(
        fetch_kbars=lambda *args: ShioajiKbarsSnapshot(
            {
                "ts": (nanos,),
                "Open": (100,),
                "High": (101,),
                "Low": (99,),
                "Close": (100,),
                "Volume": (1,),
                "Amount": (100000,),
            },
            100,
            200,
        )
    )
    plan = {**_plan(), "dataset_key": "tw_equity_minute", "trade_date": str(day)}
    request = runtime._acquire(
        member, plan, plan["parts"][0], {"release_id": "r"}, runtime.feeds[0]
    )[0]
    ContractRegistry(contracts_dir).validate("market_minute", 1, request)
    from findb_fetcher.providers.shioaji import build_market_minute_request

    batch = request["payload"]["batch"]
    expected = build_market_minute_request(
        runtime.sdk.fetch_kbars().kbars,
        dataset_key="tw_equity_minute",
        target_date=day,
        symbols=("2330",),
        fetched_at=datetime.now(timezone.utc),
        usage_before_requests=0,
        usage_after_requests=1,
        snapshot_id=batch["snapshot_id"],
        daily_update_id=batch["daily_update_id"],
        universe_id=batch["universe_id"],
    )
    assert request["request_key"] == expected["request_key"]
    assert request["idempotency_key"] == expected["idempotency_key"]
    runtime.http.close()


def test_tw_ordinary_requires_official_stock_section_cfi_excludes_four_digit_tdr() -> None:
    from findb_fetcher.full_market_universe import parse_tw_ordinary

    html = "<table><tr><td>股票</td></tr>"
    for symbol, cfi in (
        ("2330", "ESVUFR"),
        ("9103", "EDXXXX"),
        ("1234", "EPXXXX"),
        ("8888", "UNKNOWN"),
    ):
        html += f"<tr><td>{symbol} 股票</td><td>ISIN</td><td>2026/01/01</td><td>上市</td><td>產業</td><td>{cfi}</td><td></td></tr>"
    html += "<tr><td>臺灣存託憑證(TDR)</td></tr><tr><td>9105 TDR</td><td>x</td><td>x</td><td>x</td><td>x</td><td>ESVUFR</td></tr></table>"
    members = parse_tw_ordinary(html.encode("cp950"))
    assert [(member["symbol"], member["classification"]) for member in members] == [
        ("2330", "ordinary"),
        ("8888", "classification_gap"),
    ]


def test_hk_actual_equity_board_labels_and_official_rmb_iso_currency() -> None:
    raw = _hk_workbook("02/10/2026")
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(buffer, "w") as target:
        target.writestr(
            "xl/worksheets/sheet1.xml",
            source.read("xl/worksheets/sheet1.xml")
            .replace(b"Main Board", b"Equity Securities (Main Board)")
            .replace(b">GEM<", b">Equity Securities (GEM)<")
            .replace(b">CNY<", b">RMB<"),
        )
    day, members = parse_hkex(buffer.getvalue(), observed_date=date(2026, 10, 2))
    assert day == date(2026, 10, 2)
    assert [member["classification"] for member in members] == [
        "Equity Securities (Main Board)",
        "Equity Securities (GEM)",
    ]
    assert members[1]["currency"] == "CNY"
    with pytest.raises(UniverseSnapshotError, match="future-dated"):
        parse_hkex(buffer.getvalue(), observed_date=date(2026, 10, 1))


def test_operator_stop_guard_prevents_new_provider_calls(tmp_path: Path) -> None:
    runtime, source, raw = _runtime(tmp_path)
    runtime.can_acquire = lambda: False
    plan = _plan()
    runtime.state.record_plan(plan, "twelve_data")
    runtime._acquire = lambda *args: pytest.fail("operator stop must prevent acquisition")
    runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
    assert source.outcomes == [] and raw.calls == []
    runtime.http.close()
