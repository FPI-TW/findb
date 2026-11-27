"""HSIL reference documents read by the HKEX runtime: reports, workbooks and members."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from findb_fetcher import xlsx
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_errors import MemberUnavailableError, ReadinessBlockedError
from findb_fetcher.provider_adapters.base import (
    AcquisitionContext,
    FailureKind,
    UniverseContext,
)
from findb_fetcher.provider_adapters.hkex import DATASET, HkexAdapter
from findb_fetcher.provider_adapters.hsil_reference import CHANGES, CONSTITUENTS
from findb_fetcher.providers import hsil, hsil_pdf
from findb_fetcher.providers.hsil import (
    HsilFormatError,
    HsilNotPublishedError,
    HsilOperationalError,
    download_url,
    fetch_report,
    listing_url,
    report_contents,
    split_name,
    workbook_histories,
    workbook_url,
)
from findb_fetcher.raw_storage import RawObject, attach_raw_object
from hsil_support import chunk, constituent, report_pdf, sheet_rows, workbook

DAY = date(2026, 11, 27)
FETCHED = datetime(2026, 11, 27, 14, 5, tzinfo=timezone.utc)
HSCI = "Hang Seng Composite Index 恒生綜合指數"
HSCI_FIN = "Hang Seng Composite Industry Index - Financials 恒生綜合行業指數 - 金融業"
HSCI_PROP = (
    "Hang Seng Composite Industry Index - Properties & Construction 恒生綜合行業指數 - 地產建築業"
)
LARGECAP = "Hang Seng Composite LargeCap Index 恒生綜合大型股指數"
HSI = "Hang Seng Index 恒生指數"
FINANCIALS = "50 Financials 金融業"
PROPERTIES = "60 Properties and Construction 地產建築業"
HSBC = "HSBC HOLDINGS 滙豐控股"
SHKP = "SHK PPT 新鴻基地產"


def hsci_records() -> list[dict[str, list[str]]]:
    return [
        constituent(
            index=HSCI,
            code="0005.HK",
            name=HSBC,
            industry=FINANCIALS,
            close="129.9",
            weight="60.01",
        ),
        constituent(
            index=HSCI,
            code="0016.HK",
            name=SHKP,
            industry=PROPERTIES,
            close="107.7",
            weight="39.94",
        ),
        constituent(
            index=LARGECAP,
            code="0005.HK",
            name=HSBC,
            industry=FINANCIALS,
            close="129.9",
            weight="100",
        ),
        constituent(
            index=HSCI_FIN,
            code="0005.HK",
            name=HSBC,
            industry=FINANCIALS,
            close="129.9",
            weight="100",
        ),
        constituent(
            index=HSCI_PROP,
            code="0016.HK",
            name=SHKP,
            industry=PROPERTIES,
            close="107.7",
            weight="100",
        ),
    ]


def hsi_records() -> list[dict[str, list[str]]]:
    return [
        constituent(
            index=HSI,
            code="0005.HK",
            name=HSBC,
            industry=FINANCIALS,
            close="129.9",
            weight="60",
            weight_fin="100",
        ),
        constituent(
            index=HSI,
            code="0016.HK",
            name=SHKP,
            industry=PROPERTIES,
            close="107.7",
            weight="40",
            weight_prop="100",
        ),
    ]


HSCI_PDF = report_pdf("hsci", [hsci_records()[:3], hsci_records()[3:]])
HSI_PDF = report_pdf("hsi", [hsi_records()])
REPORT_RESOURCES = {
    series: f"/public-website/webapp/contents/en/indexes/report/{series}/con_27Nov26.pdf"
    for series in ("hsi", "hsci")
}


def listing(series: str, *days: date) -> bytes:
    return json.dumps(
        [
            {
                "seriesCode": series,
                "reportList": [
                    {
                        "reportType": "con-pdf-1",
                        "reportDate": [
                            {"url": REPORT_RESOURCES[series], "date": day.isoformat()}
                            for day in days
                        ],
                    },
                    {"reportType": "idx", "reportDate": []},
                ],
            }
        ]
    ).encode()


HSCI_CHANGES = [
    ["2026-10-23", 2, "Delete 刪除", "-1", "2555", "Hong Kong 香港", "CHABAIDAO", "茶百道"],
    ["2026-09-15", 3, "Add 加入", "+1", "16", "Hong Kong 香港", "SHK PPT", "新鴻基地產"],
    ["2008-09-08", 2, "Add 加入", "+2", "5", "Hong Kong 香港", "HSBC HOLDINGS", "滙豐控股"],
    ["2008-09-08", 2, "Add 加入", "+2", "2555", "Hong Kong 香港", "", ""],
]
FIN_CHANGES = [["2008-09-08", 1, "Add 加入", "+1", "5", "Hong Kong 香港", "HSBC", "滙豐"]]


def hsci_workbook(**sheets: list[list[Any]]) -> bytes:
    values = {
        "HSCI": sheet_rows("Hang Seng Composite Index", "恒生綜合指數", HSCI_CHANGES),
        "HSCI-FIN": sheet_rows(
            "Hang Seng Composite Industry Index - Financials",
            "恒生綜合行業指數 - 金融業",
            FIN_CHANGES,
        ),
        # A sheet the feeds do not read is left alone.
        "HSCI-LC": sheet_rows(
            "Hang Seng Composite LargeCap Index", "恒生綜合大型股指數", FIN_CHANGES
        ),
    }
    values.update(sheets)
    return workbook(values)


class _Publisher:
    """HSIL's listing and download service: one queue of responses per URL."""

    def __init__(self, routes: dict[str, list[httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get(str(request.url))
        if queue is None:
            return httpx.Response(404)
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _publisher(**documents: bytes) -> _Publisher:
    routes = {
        listing_url("hsi"): [httpx.Response(200, content=listing("hsi", DAY))],
        listing_url("hsci"): [httpx.Response(200, content=listing("hsci", DAY))],
        download_url(REPORT_RESOURCES["hsi"]): [
            httpx.Response(200, content=documents.get("hsi_pdf", HSI_PDF))
        ],
        download_url(REPORT_RESOURCES["hsci"]): [
            httpx.Response(200, content=documents.get("hsci_pdf", HSCI_PDF))
        ],
        workbook_url("hsci"): [
            httpx.Response(200, content=documents.get("hsci_xlsx", hsci_workbook()))
        ],
    }
    return _Publisher(routes)


class _Raw:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, raw: bytes, symbol: str, *, payload_format: str = "json") -> RawObject:
        self.calls.append((symbol, payload_format))
        digest = hashlib.sha256(raw).hexdigest()
        extension = {"pdf_gzip": "pdf.gz", "xlsx_gzip": "xlsx.gz"}[payload_format]
        return RawObject(f"r2://{'a' * 32}/raw/hkex/x/{digest}.{extension}", digest, len(raw))


def _context(
    publisher: _Publisher, dataset: str, **changes: Any
) -> tuple[AcquisitionContext, _Raw]:
    raw = _Raw()
    schema = {CONSTITUENTS: "index_constituents", CHANGES: "index_constituent_changes"}[dataset]
    values: dict[str, Any] = {
        "dataset_key": dataset,
        "trade_date": DAY,
        "fetched_at": FETCHED,
        "feed": {"provider": "hkex", "dataset_key": dataset},
        "delivery": {
            "slot_id": "asia_pacific_markets_window",
            "scheduled_for": FETCHED.isoformat(),
            "target_data_date": DAY.isoformat(),
            "work_item_id": "fp1:" + "a" * 32,
        },
        "contract": {"schema_id": schema, "current_version": 1},
        "limits": {"max_response_bytes": 64 * 1024 * 1024},
        "http": httpx.Client(transport=httpx.MockTransport(publisher)),
        "persist": raw,
        "observe_response": lambda size, declared=0: None,
        "rate_limited": lambda: None,
        "snapshot": lambda url, provider_response=False: pytest.fail("no snapshot expected"),
    }
    values.update(changes)
    return AcquisitionContext(**values), raw


def _member(symbol: str, provider_symbol: str | None = None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "provider_symbol": provider_symbol or symbol,
        "mapping_status": "mapped",
    }


def _validated(request: dict[str, Any], stored: RawObject, contracts_dir: Path) -> dict[str, Any]:
    request = {**request, "request_key": "full:x", "idempotency_key": "full:x"}
    attach_raw_object(request, stored)
    ContractRegistry(contracts_dir).validate(request["schema_id"], 1, request)
    return request


# --- constituent reports ------------------------------------------------------


def test_the_hsci_report_lists_hsci_and_its_industry_indexes(contracts_dir) -> None:
    publisher = _publisher()
    context, raw = _context(publisher, CONSTITUENTS)

    result = HkexAdapter().acquire(_member("HSCI", "hsci"), context)

    request = _validated(result.request, result.raw_object, contracts_dir)
    batch = request["payload"]["batch"]
    assert batch["delivery_mode"] == "full_snapshot" and batch["data_date"] == DAY.isoformat()
    assert batch["document"] == {
        "publisher": "hsil",
        "kind": "constituent_report",
        "series": "hsci",
        "url": download_url(REPORT_RESOURCES["hsci"]),
        "document_date": DAY.isoformat(),
        "part_key": "document",
        "original_sha256": hashlib.sha256(HSCI_PDF).hexdigest(),
        "original_bytes": len(HSCI_PDF),
        "gzip_bytes": result.raw_object.size_bytes,
    }
    assert [(item["code"], item["constituent_count"]) for item in batch["indexes"]] == [
        ("00011.00", 2),
        ("00011.08", 1),
        ("00011.09", 1),
    ]
    assert batch["indexes"][1]["parent_code"] == "00011.00"
    assert batch["indexes"][1]["classification"]["code"] == "50"
    rows = request["payload"]["data"]
    # The LargeCap index the report also lists is not delivered.
    assert [(row["index_code"], row["symbol"], row["weight_pct"]) for row in rows] == [
        ("00011.00", "00005", "60.01"),
        ("00011.00", "00016", "39.94"),
        ("00011.08", "00005", "100"),
        ("00011.09", "00016", "100"),
    ]
    assert rows[1] | {} == {
        "index_code": "00011.00",
        "trade_date": DAY.isoformat(),
        "symbol": "00016",
        "name_en": "SHK PPT",
        "name_zh": "新鴻基地產",
        "listing_place": "Hong Kong",
        "currency": "HKD",
        "close": "107.7",
        "weight_pct": "39.94",
        "industry": {
            "scheme": "hsics",
            "code": "60",
            "name_en": "Properties and Construction",
            "name_zh": "地產建築業",
        },
    }
    assert raw.calls == [("constituent_report_hsci", "pdf_gzip")]


def test_the_hsi_report_places_each_constituent_by_its_sub_index_column(contracts_dir) -> None:
    context, _ = _context(_publisher(), CONSTITUENTS)

    result = HkexAdapter().acquire(_member("HSI", "hsi"), context)

    request = _validated(result.request, result.raw_object, contracts_dir)
    assert [
        (row["index_code"], row["symbol"], row["weight_pct"]) for row in request["payload"]["data"]
    ] == [
        ("00001.00", "00005", "60"),
        ("00001.01", "00005", "100"),
        ("00001.00", "00016", "40"),
        ("00001.03", "00016", "100"),
    ]
    assert [item["code"] for item in request["payload"]["batch"]["indexes"]] == [
        "00001.00",
        "00001.01",
        "00001.03",
    ]


def test_one_report_is_fetched_and_kept_once_per_round() -> None:
    publisher = _publisher()
    context, raw = _context(publisher, CONSTITUENTS)
    adapter = HkexAdapter()

    adapter.acquire(_member("HSCI", "hsci"), context)
    adapter.acquire(_member("HSCI", "hsci"), context)
    adapter.reset()
    adapter.acquire(_member("HSCI", "hsci"), context)

    assert [str(request.url) for request in publisher.requests].count(listing_url("hsci")) == 2
    assert len(raw.calls) == 2


def test_a_report_not_yet_listed_waits_for_publication() -> None:
    publisher = _publisher()
    publisher.routes[listing_url("hsci")] = [
        httpx.Response(200, content=listing("hsci", date(2026, 11, 26)))
    ]
    context, raw = _context(publisher, CONSTITUENTS)
    adapter = HkexAdapter()

    with pytest.raises(HsilNotPublishedError) as raised:
        adapter.acquire(_member("HSCI", "hsci"), context)

    failure = adapter.classify(raised.value, "member")
    assert (failure.kind, failure.wait) == (FailureKind.ROUND_RETRY, "publication")
    # A past report the publisher no longer keeps is missing, not retried.
    assert adapter.member_outcome(raised.value) == ("missing", "no_source_row")
    assert raw.calls == []


@pytest.mark.parametrize(
    ("response", "error", "retryable"),
    [
        (httpx.Response(404), HsilNotPublishedError, None),
        (httpx.Response(503), HsilOperationalError, True),
        (httpx.Response(403), HsilOperationalError, False),
        (httpx.Response(200, content=b"<html>busy</html>"), HsilOperationalError, True),
    ],
)
def test_a_download_that_is_not_the_report_is_never_returned(response, error, retryable) -> None:
    publisher = _publisher()
    publisher.routes[download_url(REPORT_RESOURCES["hsci"])] = [response]

    with pytest.raises(error) as raised:
        fetch_report(
            httpx.Client(transport=httpx.MockTransport(publisher)),
            series="hsci",
            trade_date=DAY,
            max_response_bytes=1 << 24,
        )

    if retryable is not None:
        assert raised.value.retryable is retryable


def test_a_listing_that_names_an_unknown_location_is_rejected() -> None:
    body = listing("hsci", DAY).replace(b"/report/hsci/", b"/report/other/")
    with pytest.raises(HsilFormatError):
        hsil.report_resource(body, series="hsci", trade_date=DAY)
    with pytest.raises(HsilOperationalError):
        hsil.report_resource(b"<html>", series="hsci", trade_date=DAY)


def test_a_restarted_attempt_replays_the_journaled_report(tmp_path) -> None:
    from types import SimpleNamespace

    from findb_fetcher import provider_work

    publisher = _publisher()
    client = httpx.Client(transport=httpx.MockTransport(publisher))
    path = tmp_path / "work.sqlite3"

    def owner() -> SimpleNamespace:
        journal = provider_work.WorkJournal(path, "work")
        return SimpleNamespace(journal=journal, provider="hkex", dispatch_observations=0)

    token = provider_work._current.set(owner())
    try:
        first = fetch_report(client, series="hsci", trade_date=DAY, max_response_bytes=1 << 24)
        provider_work._current.set(owner())
        replayed = fetch_report(client, series="hsci", trade_date=DAY, max_response_bytes=1 << 24)
    finally:
        provider_work._current.reset(token)

    assert replayed == first and len(publisher.requests) == 2


# --- report layout ------------------------------------------------------------------


def test_a_report_reads_wrapped_cells_and_compatibility_ideographs() -> None:
    record = constituent(
        index=HSCI, code="0005.HK", name=HSBC, industry=FINANCIALS, close="1", weight="100"
    )
    # Some reports draw 易 of the header as the compatibility ideograph U+F9E0.
    pdf = report_pdf("hsci", [[record]]).replace(b"", b"")
    (read,) = hsil_pdf.records(pdf, "hsci")
    assert read["name"] == HSBC and read["index"] == HSCI
    assert hsil_pdf.squeeze("交易日") == "交易日"


def test_a_report_of_another_layout_is_rejected() -> None:
    record = constituent(
        index=HSCI, code="0005.HK", name=HSBC, industry=FINANCIALS, close="1", weight="100"
    )
    with pytest.raises(hsil_pdf.ReportLayoutError, match="header"):
        hsil_pdf.records(report_pdf("hsci", [[record]], header={"weight": "Other"}), "hsci")
    with pytest.raises(hsil_pdf.ReportLayoutError, match="footer"):
        hsil_pdf.records(report_pdf("hsci", [[record]], footer="Seite"), "hsci")
    with pytest.raises(HsilFormatError):
        hsil.read_report_records(b"%PDF-1.4 not a document", "hsci")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"date": ["20261126"]}, "another trade date"),
        ({"index": chunk("Hang Seng Composite Tech Index 恒生綜合科技指數")}, "unknown index"),
        ({"industry": chunk("99 Other 其他")}, "unknown industry"),
        ({"industry": chunk("50 Utilities 公用事業")}, "unknown industry"),
        ({"listed": ["Shanghai 上海"]}, "constituent shape"),
        ({"weight": ["1.23456"]}, "more decimals"),
        ({"close": ["-"]}, "unexpected number"),
    ],
)
def test_a_record_of_an_unknown_shape_rejects_the_report(change, message) -> None:
    record = constituent(
        index=HSCI, code="0005.HK", name=HSBC, industry=FINANCIALS, close="1", weight="100"
    )
    plain = {key: "".join(value) for key, value in {**record, **change}.items()}
    with pytest.raises(HsilFormatError, match=message):
        report_contents([plain], series="hsci", trade_date=DAY)


def test_an_hsi_constituent_must_be_in_exactly_one_sub_index() -> None:
    (record, _) = hsi_records()
    plain = {key: "".join(value) for key, value in record.items()}
    with pytest.raises(HsilFormatError, match="exactly one sub-index"):
        report_contents([{**plain, "weight_uti": "5"}], series="hsi", trade_date=DAY)
    del plain["weight_fin"]
    with pytest.raises(HsilFormatError, match="exactly one sub-index"):
        report_contents([plain], series="hsi", trade_date=DAY)


@pytest.mark.parametrize(
    ("cell", "names"),
    [
        ("CKH HOLDINGS 長和", ("CKH HOLDINGS", "長和")),
        ("TCL ELECTRONICS TCL電子", ("TCL ELECTRONICS", "TCL電子")),
        ("SENSETIME ‑ W 商湯 ‑ W", ("SENSETIME - W", "商湯 - W")),
        ("SWIRE PACIFIC A 太古股份公司A", ("SWIRE PACIFIC A", "太古股份公司A")),
        ("MANYCORE TECH MANYCORE TECH", ("MANYCORE TECH", "MANYCORE TECH")),
        ("長和", (None, "長和")),
        ("SOME NAME OTHER", (None, None)),
    ],
)
def test_a_name_cell_splits_only_where_it_is_unambiguous(cell, names) -> None:
    assert split_name(cell) == names


# --- change workbooks ---------------------------------------------------------------


def test_a_workbook_sheet_is_one_index_history_newest_first() -> None:
    histories = workbook_histories(hsci_workbook(), "hsci")

    assert sorted(histories) == ["00011.00", "00011.08"]
    history = histories["00011.00"]
    assert (history.history_start, history.latest) == (date(2008, 9, 8), date(2026, 10, 23))
    assert history.rows[0] == {
        "ordinal": 1,
        "effective_date": "2026-10-23",
        "action": "delete",
        "symbol": "02555",
        "listing_place": "Hong Kong",
        "constituents_after": 2,
        "count_change": -1,
        "name_en": "CHABAIDAO",
        "name_zh": "茶百道",
    }
    # A change printed without names carries none.
    assert "name_en" not in history.rows[3] and history.rows[3]["ordinal"] == 4


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            sheet_rows(
                "Hang Seng Composite Index",
                "恒生綜合指數",
                [["2026-10-23", 2, "Moved", "-1", "1", "Hong Kong 香港", "A", "甲"]],
            ),
            "change shape",
        ),
        (sheet_rows("Hang Seng Index", "恒生指數", HSCI_CHANGES), "another index"),
        (sheet_rows("Hang Seng Composite Index", "恒生綜合指數", [])[:6], "lists no change"),
        (
            sheet_rows("Hang Seng Composite Index", "恒生綜合指數", HSCI_CHANGES[:1])
            + [HSCI_CHANGES[1]],
            "interleaves",
        ),
    ],
)
def test_a_sheet_of_an_unknown_shape_rejects_the_workbook(rows, message) -> None:
    with pytest.raises(HsilFormatError, match=message):
        workbook_histories(hsci_workbook(HSCI=rows), "hsci")


def test_each_index_is_delivered_from_its_own_sheet(contracts_dir) -> None:
    publisher = _publisher()
    context, raw = _context(publisher, CHANGES)
    adapter = HkexAdapter()

    result = adapter.acquire(_member("00011.08"), context)
    root = adapter.acquire(_member("00011.00"), context)

    request = _validated(result.request, result.raw_object, contracts_dir)
    batch = request["payload"]["batch"]
    # The workbook stands for its latest effective date, whichever sheet is delivered.
    assert batch["document"]["document_date"] == "2026-10-23"
    assert batch["document"]["part_key"] == "00011.08"
    assert batch["index"]["classification"]["code"] == "50"
    assert batch["history_start"] == "2008-09-08"
    assert batch["declared_record_count"] == 1
    _validated(root.request, root.raw_object, contracts_dir)
    assert raw.calls == [("change_workbook_hsci", "xlsx_gzip")]
    assert [str(request.url) for request in publisher.requests] == [workbook_url("hsci")]

    with pytest.raises(MemberUnavailableError) as missing:
        adapter.acquire(_member("00011.14"), context)
    assert missing.value.code == "missing_symbol"


def test_a_workbook_that_speaks_for_a_later_day_is_not_delivered_earlier() -> None:
    context, _ = _context(_publisher(), CHANGES, trade_date=date(2026, 10, 22))

    with pytest.raises(MemberUnavailableError) as raised:
        HkexAdapter().acquire(_member("00011.08"), context)

    assert raised.value.code == "document_later_than_date"


def test_the_shared_xlsx_reader_keeps_workbook_order() -> None:
    raw = workbook({"B": [["second"]], "A": [["first"]]})
    assert list(xlsx.sheets(raw)) == ["B", "A"]
    assert xlsx.first_sheet_rows(raw) == [{"A": "second"}]
    with pytest.raises(xlsx.XlsxError):
        xlsx.sheets(b"PK not a workbook")


# --- members and failures --------------------------------------------------------------


def _universe_context(snapshots: dict[str, bytes]) -> UniverseContext:
    zones = {key: ZoneInfo("Asia/Hong_Kong") for key in (DATASET, CONSTITUENTS, CHANGES)}
    return UniverseContext(
        dataset_keys=[CONSTITUENTS, CHANGES],
        observed_at=FETCHED,
        zones=zones,
        snapshot=lambda url, provider_response=False: snapshots[url],
        catalogue=lambda **kwargs: pytest.fail("no provider catalogue"),
        catalogue_evidence=lambda url: pytest.fail("no provider catalogue"),
        persist=lambda *args, **kwargs: pytest.fail("evidence is persisted by the runtime"),
        observe_response=lambda *args, **kwargs: None,
        snapshot_objects={},
        refresh_limits=lambda: None,
        pace=lambda: None,
        target_date_lags={CONSTITUENTS: 0, CHANGES: 0},
    )


def test_the_reference_datasets_have_fixed_members() -> None:
    snapshots = {listing_url(series): listing(series, DAY) for series in ("hsi", "hsci")}
    feeds = [{"dataset_key": CONSTITUENTS}, {"dataset_key": CHANGES}]

    groups = HkexAdapter().build_universe(feeds, _universe_context(snapshots))

    assert set(groups) == {CONSTITUENTS, CHANGES}
    assert [member["symbol"] for member in groups[CONSTITUENTS].members] == ["HSI", "HSCI"]
    assert [member["symbol"] for member in groups[CHANGES].members] == [
        index.code for index in hsil.INDEXES
    ]
    assert len(groups[CHANGES].members) == 18
    assert {member["mapping_status"] for member in groups[CHANGES].members} == {"mapped"}
    assert groups[CONSTITUENTS].effective_date == DAY
    assert groups[CONSTITUENTS].evidence == snapshots
    with pytest.raises(ReadinessBlockedError, match="no universe"):
        HkexAdapter().build_universe([{"dataset_key": "hk_other"}], _universe_context(snapshots))


@pytest.mark.parametrize(
    ("exc", "kind", "wait"),
    [
        (HsilNotPublishedError("404"), FailureKind.ROUND_RETRY, "publication"),
        (HsilOperationalError("503", status_code=503), FailureKind.ROUND_RETRY, "outage"),
        (HsilOperationalError("page"), FailureKind.ROUND_RETRY, "outage"),
        (HsilOperationalError("403", status_code=403), FailureKind.WORK_FAILED, None),
        (HsilFormatError("layout"), FailureKind.WORK_FAILED, None),
    ],
)
def test_reference_failures_map_like_the_exchange_reports(exc, kind, wait) -> None:
    adapter = HkexAdapter()
    for phase in ("member", "round"):
        failure = adapter.classify(exc, phase)
        assert (failure.kind, failure.wait) == (kind, wait)
    assert adapter.classify(exc, "maintenance").kind == FailureKind.PASSTHROUGH
    assert adapter.diagnostic(exc) is not None
