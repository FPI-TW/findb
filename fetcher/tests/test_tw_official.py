"""TWSE and TPEx: each exchange's own index from its public month endpoint."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_errors import ReadinessBlockedError
from findb_fetcher.provider_adapters.base import (
    AcquisitionContext,
    FailureKind,
    UniverseContext,
)
from findb_fetcher.provider_adapters.tpex import TpexAdapter
from findb_fetcher.provider_adapters.twse import TwseAdapter
from findb_fetcher.providers import tpex, tw_official, twse
from findb_fetcher.providers.tw_official import (
    STOCK_COLUMNS,
    TwOfficialFormatError,
    TwOfficialNotPublishedError,
    TwOfficialPayloadError,
    TwOfficialResponseError,
)
from findb_fetcher.raw_storage import RawObject, attach_raw_object

FIXTURES = Path(__file__).parent / "fixtures" / "tw_official"
TWSE_MONTH = (FIXTURES / "twse_index_202611.json").read_bytes()
TPEX_MONTH = (FIXTURES / "tpex_index_202611.json").read_bytes()
TWSE_EMPTY = json.dumps({"stat": "很抱歉，沒有符合條件的資料!", "total": 0}).encode()
TUESDAY = date(2026, 11, 24)
WEDNESDAY = date(2026, 11, 25)
# 16:00 Taipei on the Tuesday.
FETCHED = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)
TAIPEI = ZoneInfo("Asia/Taipei")


@pytest.fixture(autouse=True)
def _installed_exchanges(monkeypatch):
    """The provider rows the catalog will publish; neither is in the packaged catalog yet."""
    from findb_fetcher import execution_metadata

    installed = execution_metadata._installed_metadata

    def with_exchanges() -> dict[str, Any]:
        value = installed()
        rows = [
            {
                "provider": provider,
                "credential_env_names": [],
                "platform_limits": {"requests_per_minute": 20, "max_response_bytes": 1 << 24},
            }
            for provider in ("twse", "tpex")
        ]
        return {**value, "providers": [*value["providers"], *rows]}

    monkeypatch.setattr(execution_metadata, "_installed_metadata", with_exchanges)


class _Exchange:
    """One exchange behind a MockTransport: a queue of responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _ok(body: bytes, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, content=body, headers=headers)


class _Raw:
    def __init__(self, provider: str, dataset: str) -> None:
        self.provider, self.dataset = provider, dataset
        self.calls: list[tuple[bytes, str]] = []

    def __call__(self, raw: bytes, symbol: str, *, payload_format: str = "json") -> RawObject:
        self.calls.append((raw, symbol))
        digest = hashlib.sha256(raw).hexdigest()
        return RawObject(
            f"r2://{'a' * 32}/raw/{self.provider}/{self.dataset}/{digest}.json", digest, len(raw)
        )


ADAPTERS = {
    "twse": (TwseAdapter, "tw_twse_index_eod", "^TWII", TWSE_MONTH),
    "tpex": (TpexAdapter, "tw_tpex_index_eod", "^TWOII", TPEX_MONTH),
}


def _context(provider: str, server: _Exchange, day: date = TUESDAY, **changes: Any):
    _, dataset, _, _ = ADAPTERS[provider]
    raw, observed = _Raw(provider, dataset), []
    values: dict[str, Any] = {
        "dataset_key": dataset,
        "trade_date": day,
        "fetched_at": FETCHED,
        "feed": {"provider": provider, "dataset_key": dataset},
        "delivery": {
            "slot_id": "taiwan_market_window",
            "scheduled_for": FETCHED.isoformat(),
            "target_data_date": day.isoformat(),
            "work_item_id": "fp1:" + "a" * 32,
        },
        "contract": {"schema_id": "market_eod", "current_version": 2},
        "limits": {"max_response_bytes": 1 << 24},
        "http": httpx.Client(transport=httpx.MockTransport(server)),
        "persist": raw,
        "observe_response": lambda size, declared=0: observed.append((size, declared)),
        "rate_limited": lambda: observed.append("rate_limited"),
        "snapshot": lambda url, provider_response=False: pytest.fail("no snapshot expected"),
    }
    values.update(changes)
    return AcquisitionContext(**values), raw, observed


def _member(symbol: str, exchange: str) -> dict[str, Any]:
    return {
        "member_key": symbol,
        "symbol": symbol,
        "provider_symbol": symbol,
        "exchange": exchange,
        "currency": "TWD",
        "asset_class": "index",
        "mapping_status": "mapped",
    }


def test_twse_month_rows_are_read_from_minguo_dates() -> None:
    rows = twse.index_rows(TWSE_MONTH)

    assert len(rows) == 17 and min(rows) == date(2026, 11, 2) and max(rows) == TUESDAY
    assert rows[TUESDAY] == {
        "open": Decimal("39148.91"),
        "high": Decimal("40180.38"),
        "low": Decimal("39148.91"),
        "close": Decimal("40122.35"),
    }
    assert twse.index_rows(TWSE_EMPTY) == {}
    with pytest.raises(TwOfficialNotPublishedError):
        twse.index_row(TWSE_MONTH, WEDNESDAY)


def test_tpex_month_rows_are_read_and_their_month_is_checked() -> None:
    rows = tpex.index_rows(TPEX_MONTH, TUESDAY)

    assert len(rows) == 17
    assert rows[TUESDAY]["close"] == Decimal("293.23")
    with pytest.raises(TwOfficialNotPublishedError):
        tpex.index_row(TPEX_MONTH, WEDNESDAY)
    with pytest.raises(TwOfficialFormatError):
        tpex.index_rows(TPEX_MONTH, date(2026, 10, 30))


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda value: value.update(fields=["日期", "開盤", "最高", "最低", "收盤"]), "format"),
        (lambda value: value["data"].append(value["data"][0]), "format"),
        (lambda value: value["data"][0].__setitem__(4, "n/a"), "format"),
        (lambda value: value["data"][0].__setitem__(0, "115/02/30"), "format"),
        (lambda value: value.update(stat="查詢日期大於今日"), "payload"),
    ],
)
def test_a_changed_twse_response_is_refused(change, error) -> None:
    value = json.loads(TWSE_MONTH)
    change(value)
    expected = TwOfficialFormatError if error == "format" else TwOfficialPayloadError
    with pytest.raises(expected):
        twse.index_rows(json.dumps(value, ensure_ascii=False).encode())


def test_a_block_page_is_a_payload_error_not_a_format_change() -> None:
    with pytest.raises(TwOfficialPayloadError):
        twse.index_rows(b"<html><body>The page cannot be accessed</body></html>")
    with pytest.raises(TwOfficialPayloadError):
        tpex.index_rows(b"[]", TUESDAY)


def test_displayed_values_parse_exactly() -> None:
    assert tw_official.roc_date("115/11/24") == TUESDAY
    assert tw_official.roc_date("89/01/04") == date(2000, 1, 4)
    # TWSE pads two-digit Minguo years in older months.
    assert tw_official.roc_date(" 88/06/01") == date(1999, 6, 1)
    assert tw_official.slash_date("2026/11/24") == TUESDAY
    assert tw_official.number("-1,234.50") == Decimal("-1234.50")
    for bad in ("", "1.2.3", "--", "1,2a"):
        with pytest.raises(TwOfficialFormatError):
            tw_official.number(bad)


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_the_index_row_becomes_a_market_eod_v2_request(provider, contracts_dir) -> None:
    adapter_class, dataset, symbol, month = ADAPTERS[provider]
    server = _Exchange(_ok(month))
    context, raw, _ = _context(provider, server)

    result = adapter_class().acquire(_member(symbol, provider.upper()), context)

    assert result.request is not None and result.raw_object is not None
    [row] = result.request["payload"]["data"]
    assert row["symbol"] == symbol and row["source_symbol"] == symbol
    assert row["trade_date"] == "2026-11-24" and row["currency"] == "TWD"
    assert "volume" not in row
    assert result.request["source"] == provider and result.request["dataset_key"] == dataset
    assert raw.calls == [(month, symbol)]
    request = {**result.request, "request_key": "full:x", "idempotency_key": "full:x"}
    attach_raw_object(request, result.raw_object)
    ContractRegistry(contracts_dir).validate("market_eod", 2, request)
    sent = server.requests[0]
    assert sent.url.host == ("www.twse.com.tw" if provider == "twse" else "www.tpex.org.tw")
    assert dict(sent.url.params)["date"] == ("20261101" if provider == "twse" else "2026/11/01")


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_one_month_response_serves_the_other_days_of_the_round(provider) -> None:
    adapter_class, _, symbol, month = ADAPTERS[provider]
    server = _Exchange(_ok(month))
    adapter = adapter_class()
    for day in (date(2026, 11, 20), date(2026, 11, 23), TUESDAY):
        context, _, _ = _context(provider, server, day)
        assert adapter.acquire(_member(symbol, provider.upper()), context).request
    assert len(server.requests) == 1

    # A day the kept month does not hold is fetched again; reset drops the month.
    context, _, _ = _context(provider, server, WEDNESDAY)
    with pytest.raises(TwOfficialNotPublishedError):
        adapter.acquire(_member(symbol, provider.upper()), context)
    assert len(server.requests) == 2
    adapter.reset()
    context, _, _ = _context(provider, server)
    adapter.acquire(_member(symbol, provider.upper()), context)
    assert len(server.requests) == 3


def test_an_adapter_serves_only_its_own_index() -> None:
    context, _, _ = _context("twse", _Exchange(_ok(TWSE_MONTH)))
    with pytest.raises(ReadinessBlockedError):
        TwseAdapter().acquire(_member("^TWOII", "TWSE"), context)
    other, _, _ = _context("twse", _Exchange(_ok(TWSE_MONTH)), dataset_key="tw_tpex_index_eod")
    with pytest.raises(ReadinessBlockedError):
        TwseAdapter().acquire(_member("^TWII", "TWSE"), other)


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (403, "blocked", True),
        (429, "rate_limited", True),
        (503, "server_error", True),
        (404, "http_error", False),
    ],
)
def test_http_failures_keep_a_bounded_category(status, category, retryable) -> None:
    server = _Exchange(_ok(b"<html>busy</html>", status, **{"Retry-After": "30"}))
    context, _, observed = _context("twse", server)

    with pytest.raises(TwOfficialResponseError) as failed:
        TwseAdapter().acquire(_member("^TWII", "TWSE"), context)

    assert (failed.value.category, failed.value.retryable) == (category, retryable)
    assert failed.value.retry_after == 30
    assert ("rate_limited" in observed) == (status == 429)
    failure = TwseAdapter().classify(failed.value, "member")
    if retryable:
        assert (failure.kind, failure.wait, failure.retry_after) == (
            FailureKind.ROUND_RETRY,
            "outage",
            30,
        )
    else:
        assert failure.kind is FailureKind.WORK_FAILED


def test_failures_are_classified_for_daily_and_historical_work() -> None:
    adapter = TpexAdapter()
    unpublished = TwOfficialNotPublishedError("tpex_index_not_published")
    waiting = adapter.classify(unpublished, "member")
    assert (waiting.kind, waiting.wait) == (FailureKind.ROUND_RETRY, "publication")
    assert adapter.member_outcome(unpublished) == ("missing", "no_source_row")
    changed = adapter.classify(TwOfficialFormatError("x"), "member")
    assert (changed.kind, changed.code) == (FailureKind.WORK_FAILED, "tpex_format_changed")
    blocked = adapter.classify(TwOfficialPayloadError("x"), "member")
    assert (blocked.kind, blocked.wait) == (FailureKind.ROUND_RETRY, "outage")
    assert adapter.classify(TwOfficialPayloadError("x"), "maintenance").kind is (
        FailureKind.ROUND_RETRY
    )
    assert adapter.classify(TwOfficialFormatError("x"), "maintenance").kind is (
        FailureKind.WORK_FAILED
    )
    assert adapter.classify(ValueError("x"), "member").kind is FailureKind.PASSTHROUGH
    assert adapter.diagnostic(TwOfficialFormatError("x")) == {"category": "format_changed"}


def _universe_context(server: _Exchange, dataset: str, **changes: Any) -> UniverseContext:
    values: dict[str, Any] = {
        "dataset_keys": [dataset],
        "observed_at": FETCHED,
        "zones": {dataset: TAIPEI},
        "snapshot": lambda url, provider_response=False: pytest.fail("no snapshot expected"),
        "catalogue": lambda dataset_keys=(): [],
        "catalogue_evidence": lambda url: {},
        "persist": lambda *args, **kwargs: pytest.fail("the runtime persists evidence"),
        "observe_response": lambda size, declared=0: None,
        "snapshot_objects": {},
        "refresh_limits": lambda: None,
        "pace": lambda: None,
        "http": httpx.Client(transport=httpx.MockTransport(server)),
        "target_date_lags": {dataset: 0},
    }
    values.update(changes)
    return UniverseContext(**values)


@pytest.mark.parametrize("provider", sorted(ADAPTERS))
def test_the_universe_is_the_exchange_index_evidenced_by_its_month(provider) -> None:
    adapter_class, dataset, symbol, month = ADAPTERS[provider]
    server = _Exchange(_ok(month))

    snapshot = adapter_class().build_universe(
        [{"dataset_key": dataset}], _universe_context(server, dataset)
    )[dataset]

    assert snapshot.effective_date == TUESDAY
    assert [member["symbol"] for member in snapshot.members] == [symbol]
    assert snapshot.members[0]["asset_class"] == "index"
    assert snapshot.members[0]["currency"] == "TWD"
    [(url, evidence)] = snapshot.evidence.items()
    assert evidence == month
    assert url == (twse if provider == "twse" else tpex).index_month_url(TUESDAY)


def test_the_universe_needs_the_published_lag_and_a_known_dataset() -> None:
    server = _Exchange(_ok(TWSE_MONTH))
    with pytest.raises(ReadinessBlockedError):
        TwseAdapter().build_universe(
            [{"dataset_key": "tw_twse_index_eod"}],
            _universe_context(server, "tw_twse_index_eod", target_date_lags={}),
        )
    with pytest.raises(ReadinessBlockedError):
        TwseAdapter().build_universe(
            [{"dataset_key": "tw_equity_eod"}], _universe_context(server, "tw_equity_eod")
        )


# --- Three institutional investors tables -------------------------------------

FRIDAY = date(2026, 11, 20)
TWSE_SUMMARY = (FIXTURES / "twse_summary_20261120.json").read_bytes()
TPEX_SUMMARY = (FIXTURES / "tpex_summary_20261120.json").read_bytes()
TWSE_STOCK = (FIXTURES / "twse_stock_20261120.json").read_bytes()
TPEX_STOCK = (FIXTURES / "tpex_stock_20261120.json").read_bytes()
TWSE_NONE = json.dumps({"stat": "很抱歉，沒有符合條件的資料!", "total": 0}).encode()

TABLES = {
    ("twse", "summary"): (TwseAdapter, "tw_twse_institutional_summary", "BFI82U", TWSE_SUMMARY),
    ("tpex", "summary"): (
        TpexAdapter,
        "tw_tpex_institutional_summary",
        "insti_summary",
        TPEX_SUMMARY,
    ),
    ("twse", "stock"): (TwseAdapter, "tw_twse_institutional_stock", "T86", TWSE_STOCK),
    ("tpex", "stock"): (
        TpexAdapter,
        "tw_tpex_institutional_stock",
        "insti_daily_trade",
        TPEX_STOCK,
    ),
}


def _edit(raw: bytes, change) -> bytes:
    value = json.loads(raw)
    change(value)
    return json.dumps(value, ensure_ascii=False).encode()


def _board_member(provider: str, source_symbol: str) -> dict[str, Any]:
    return {
        "member_key": provider.upper(),
        "symbol": provider.upper(),
        "provider_symbol": source_symbol,
        "exchange": provider.upper(),
        "currency": "TWD",
        "asset_class": "equity",
        "mapping_status": "mapped",
    }


def test_both_summaries_map_every_printed_category() -> None:
    day, twse_rows = twse.summary_categories(TWSE_SUMMARY, FRIDAY)
    assert day == FRIDAY
    assert {row["category"]: row["net_amount"] for row in twse_rows} == {
        "dealer_proprietary": -5_067_838_740,
        "dealer_hedge": -29_015_401_380,
        "investment_trust": -7_346_992_003,
        "foreign_ex_dealer": -9_510_543_572,
        "foreign_dealer": 0,
        "total": -50_940_775_695,
    }
    _, tpex_rows = tpex.summary_categories(TPEX_SUMMARY, FRIDAY)
    categories = {row["category"] for row in tpex_rows}
    assert categories == {
        "foreign_total",
        "foreign_ex_dealer",
        "foreign_dealer",
        "investment_trust",
        "dealer_total",
        "dealer_proprietary",
        "dealer_hedge",
        "total",
    }


def test_both_stock_tables_map_to_the_published_layout() -> None:
    _, twse_rows = twse.stock_rows(TWSE_STOCK, FRIDAY)
    assert len(twse_rows) == 5
    first = twse_rows[0]
    assert (first["symbol"], first["name"], first["total_net"]) == ("3481", "群創", 49_349_043)
    assert first["foreign_total_net"] is None and first["dealer_total_buy"] is None
    assert first["dealer_total_net"] == first["dealer_proprietary_net"] + first["dealer_hedge_net"]
    _, tpex_rows = tpex.stock_rows(TPEX_STOCK, FRIDAY)
    assert [row["symbol"] for row in tpex_rows] == [
        "00411A",
        "006201",
        "00679B",
        "700024",
        "700126",
    ]
    warrant = tpex_rows[3]
    assert (warrant["dealer_total_buy"], warrant["dealer_total_net"]) == (4000, -66000)
    for row in (*twse_rows, *tpex_rows):
        assert set(row) == set(STOCK_COLUMNS)
    rows_object = json.loads(tw_official.rows_object(tpex_rows))
    assert rows_object["columns"] == list(STOCK_COLUMNS)
    assert rows_object["rows"][3][:2] == ["700024", "新應材統一64購02"]


@pytest.mark.parametrize(
    ("parser", "raw"),
    [
        (twse.summary_categories, TWSE_NONE),
        (twse.stock_rows, TWSE_NONE),
        # An unpublished day is answered with the latest one.
        (twse.summary_categories, TWSE_SUMMARY),
        (tpex.summary_categories, TPEX_SUMMARY),
        (tpex.stock_rows, _edit(TPEX_STOCK, lambda value: value["tables"][0].update(data=[]))),
    ],
)
def test_a_table_of_another_day_or_none_is_not_published(parser, raw) -> None:
    with pytest.raises(TwOfficialNotPublishedError):
        parser(raw, date(2026, 11, 23))


@pytest.mark.parametrize(
    ("parser", "raw"),
    [
        (
            twse.stock_rows,
            _edit(TWSE_STOCK, lambda value: value["fields"].__setitem__(4, "外陸資買賣超")),
        ),
        (twse.stock_rows, _edit(TWSE_STOCK, lambda value: value.update(total=6))),
        (tpex.stock_rows, _edit(TPEX_STOCK, lambda value: value["tables"][0].update(totalCount=9))),
        (
            twse.summary_categories,
            _edit(
                TWSE_SUMMARY, lambda value: value["data"].__setitem__(0, ["自營商", "1", "1", "0"])
            ),
        ),
        (
            tpex.summary_categories,
            _edit(TPEX_SUMMARY, lambda value: value["tables"][0]["data"].pop()),
        ),
        (
            tpex.stock_rows,
            _edit(TPEX_STOCK, lambda value: value["tables"][0]["data"][0].__setitem__(2, "1.5")),
        ),
        (
            twse.stock_rows,
            _edit(TWSE_STOCK, lambda value: value["data"][0].__setitem__(0, "33-1")),
        ),
    ],
)
def test_a_changed_table_is_a_format_change(parser, raw) -> None:
    with pytest.raises(TwOfficialFormatError):
        parser(raw, FRIDAY)


def test_the_rows_object_layout_is_the_contracts(contracts_dir) -> None:
    schema = json.loads((contracts_dir / "institutional_flow_stock" / "v1.schema.json").read_text())
    columns = [column["name"] for column in schema["x-findb-row-layout"]["columns"]]
    assert columns == list(STOCK_COLUMNS)
    assert schema["x-findb-row-layout"]["format"] == "json"


@pytest.mark.parametrize("provider", ["twse", "tpex"])
def test_a_summary_is_one_inline_request_of_the_board(provider, contracts_dir) -> None:
    adapter_class, dataset, source_symbol, raw = TABLES[(provider, "summary")]
    server = _Exchange(_ok(raw))
    context, persisted, _ = _context(
        provider,
        server,
        FRIDAY,
        dataset_key=dataset,
        contract={"schema_id": "institutional_flow_summary", "current_version": 1},
    )

    result = adapter_class().acquire(_board_member(provider, source_symbol), context)

    assert result.request is not None and result.raw_object is not None
    assert result.request["schema_id"] == "institutional_flow_summary"
    [row] = result.request["payload"]["data"]
    assert (row["symbol"], row["source_symbol"], row["currency"]) == (
        provider.upper(),
        source_symbol,
        "TWD",
    )
    assert row["trade_date"] == FRIDAY.isoformat()
    assert len(row["categories"]) == (6 if provider == "twse" else 8)
    assert persisted.calls == [(raw, source_symbol)]
    request = {**result.request, "request_key": "full:x", "idempotency_key": "full:x"}
    attach_raw_object(request, result.raw_object)
    ContractRegistry(contracts_dir).validate("institutional_flow_summary", 1, request)
    sent = server.requests[0]
    expected = {"dayDate": "20261120"} if provider == "twse" else {"date": "2026/11/20"}
    assert expected.items() <= dict(sent.url.params).items()


@pytest.mark.parametrize("provider", ["twse", "tpex"])
def test_a_stock_table_is_delivered_by_reference(provider, contracts_dir) -> None:
    adapter_class, dataset, source_symbol, raw = TABLES[(provider, "stock")]
    server = _Exchange(_ok(raw))
    context, persisted, _ = _context(
        provider,
        server,
        FRIDAY,
        dataset_key=dataset,
        contract={"schema_id": "institutional_flow_stock", "current_version": 1},
    )

    result = adapter_class().acquire(_board_member(provider, source_symbol), context)

    assert result.request is not None
    [row] = result.request["payload"]["data"]
    (original, _), (rows_body, symbol) = persisted.calls
    assert original == raw and symbol == source_symbol
    rows_object = json.loads(rows_body)
    assert rows_object["columns"] == list(STOCK_COLUMNS) and len(rows_object["rows"]) == 5
    assert row["row_count"] == 5 and row["rows_bytes"] == len(rows_body)
    assert row["rows_format"] == "json" and row["volume_unit"] == "share"
    assert row["rows_ref"].endswith(f"{row['rows_sha256']}.json")
    assert result.raw_object.sha256 != row["rows_sha256"]
    request = {**result.request, "request_key": "full:x", "idempotency_key": "full:x"}
    attach_raw_object(request, result.raw_object)
    ContractRegistry(contracts_dir).validate("institutional_flow_stock", 1, request)
    if provider == "twse":
        # Warrants are kept: every TWSE class is requested.
        assert dict(server.requests[0].url.params)["selectType"] == "ALL"


def test_an_unpublished_table_waits_and_a_past_one_is_missing() -> None:
    adapter_class, dataset, source_symbol, _ = TABLES[("twse", "stock")]
    server = _Exchange(_ok(TWSE_NONE))
    context, persisted, _ = _context(
        "twse",
        server,
        FRIDAY,
        dataset_key=dataset,
        contract={"schema_id": "institutional_flow_stock", "current_version": 1},
    )
    adapter = adapter_class()
    with pytest.raises(TwOfficialNotPublishedError) as unpublished:
        adapter.acquire(_board_member("twse", source_symbol), context)
    # The response is kept as evidence even though it is not the day's table.
    assert persisted.calls == [(TWSE_NONE, source_symbol)]
    failure = adapter.classify(unpublished.value, "member")
    assert (failure.kind, failure.wait) == (FailureKind.ROUND_RETRY, "publication")
    assert adapter.member_outcome(unpublished.value) == ("missing", "no_source_row")


def test_only_the_boards_own_table_member_is_served() -> None:
    _, dataset, _, raw = TABLES[("tpex", "summary")]
    context, _, _ = _context(
        "tpex",
        _Exchange(_ok(raw)),
        FRIDAY,
        dataset_key=dataset,
        contract={"schema_id": "institutional_flow_summary", "current_version": 1},
    )
    with pytest.raises(ReadinessBlockedError):
        TpexAdapter().acquire(_board_member("twse", "insti_summary"), context)
    with pytest.raises(ReadinessBlockedError):
        TpexAdapter().acquire(_board_member("tpex", "insti_daily_trade"), context)


@pytest.mark.parametrize(("provider", "kind"), sorted(TABLES))
def test_a_table_universe_is_the_board_evidenced_by_the_latest_table(provider, kind) -> None:
    adapter_class, dataset, source_symbol, raw = TABLES[(provider, kind)]
    server = _Exchange(_ok(raw))

    snapshot = adapter_class().build_universe(
        [{"dataset_key": dataset}], _universe_context(server, dataset)
    )[dataset]

    [member] = snapshot.members
    assert (member["symbol"], member["provider_symbol"], member["asset_class"]) == (
        provider.upper(),
        source_symbol,
        "equity",
    )
    [(url, evidence)] = snapshot.evidence.items()
    assert evidence == raw
    # Asked without a day: the exchange answers with its latest published table.
    params = dict(server.requests[0].url.params)
    assert "date" not in params and "dayDate" not in params
    module = twse if provider == "twse" else tpex
    assert url == (module.summary_url if kind == "summary" else module.stock_url)(None)
