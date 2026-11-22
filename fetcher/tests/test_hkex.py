"""HKEX daily quotations: intact range downloads, a strict parser and one raw per report."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_errors import MemberUnavailableError
from findb_fetcher.full_market_universe import HKEX_URL, official_snapshot_dataset
from findb_fetcher.provider_adapters.base import (
    AcquisitionContext,
    FailureKind,
    UniverseContext,
)
from findb_fetcher.provider_adapters.hkex import DATASET, HOLIDAY_EXCERPT, HkexAdapter
from findb_fetcher.providers import hkex
from findb_fetcher.providers.hkex import (
    HkexOperationalError,
    HkexReportFormatError,
    HkexReportNotPublishedError,
    compress,
    fetch_report,
    parse_report,
    report_url,
)
from findb_fetcher.raw_storage import RawObject, attach_raw_object
from test_full_market import _hk_workbook

DAY = date(2026, 11, 20)
FETCHED = datetime(2026, 11, 20, 13, 5, tzinfo=timezone.utc)
LAST_MODIFIED = "Fri, 20 Nov 2026 12:00:04 GMT"
MAIN_URL = report_url("main", DAY)
GEM_URL = report_url("gem", DAY)
HOLIDAY = b"""<html><body><span id="Title3">Daily Quotations</span>
<pre> Hong Kong Public Holiday, NO TRADING.

</pre></body></html>"""


def _quote(code: str, name: str, first: tuple[str, ...], second: tuple[str, ...], mark=" "):
    # The name column is 16 characters wide once entities are decoded.
    name += " " * (16 - len(html.unescape(name)))
    return (
        f"{code:>6}{mark}{name} HKD {first[0]:>8} {first[1]:>8} {first[2]:>8} {first[3]:>20}\n"
        f"{'':>27} {second[0]:>8} {second[1]:>8} {second[2]:>8} {second[3]:>20}\n"
    )


def _report(title: str = "MAIN BOARD AND TRADING ONLY STOCKS", day: str = "20 NOV 2026") -> bytes:
    body = (
        f"<html><pre>                       THE STOCK EXCHANGE OF HONG KONG LIMITED\n"
        f"EXCHANGE SQUARE, HONG KONG      TEL: 25221122         DATE: {day} (FRIDAY)\n"
        f"                        {title}\n"
        '<a name = "quotations">QUOTATIONS</a>\n\n'
        " CODE  NAME OF STOCK    CUR PRV.CLO./    ASK/    HIGH/      SHARES TRADED/\n"
        "                            CLOSING      BID     LOW        TURNOVER ($)\n\n"
    )
    if title.startswith("MAIN"):
        body += _quote(
            "3",
            "HK &amp; CHINA GAS",
            ("7.435", "7.335", "7.45", "27,126,575"),
            ("7.33", "7.33", "7.27", "198,845,509"),
        )
        body += _quote(
            "25", "CHEVALIER INT'L", ("4.40", "4.76", "-", "-"), ("4.40", "4.32", "-", "-")
        )
        body += "    36 FE HLDGS INTL    HKD TRADING SUSPENDED\n"
        body += "   690#UNI-BIO GROUP    HKD TRADING HALTED\n"
        body += _quote(
            "700",
            "TENCENT",
            ("348.40", "344.00", "349.60", "41,094,469"),
            ("344.00", "343.80", "339.60", "14,089,992,865"),
        )
    else:
        body += _quote(
            "8003",
            "GREAT WORLD",
            ("0.09", "0.103", "0.102", "124,000"),
            ("0.101", "0.09", "0.095", "12,414"),
        )
    return (
        body + "\n-------------------------------------------------------\n</pre></html>"
    ).encode("latin-1")


MAIN = _report()
GEM = _report("GROWTH ENTERPRISE MARKET")


def _respond(body: bytes, *, extra: bytes = b"", status: int = 206) -> httpx.Response:
    return httpx.Response(
        status,
        content=body + extra,
        headers={
            "Content-Range": f"bytes 0-{len(body) - 1}/{len(body)}",
            "Last-Modified": LAST_MODIFIED,
            "ETag": '"0a7b6aee678dd1:0"',
        },
    )


class _Exchange:
    """The exchange behind a CDN: one queue of responses per report URL."""

    def __init__(self, **queues: list[httpx.Response]) -> None:
        self.queues = {MAIN_URL: queues.get("main", []), GEM_URL: queues.get("gem", [])}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.queues[str(request.url.copy_with(query=None))]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def urls(self) -> list[str]:
        return [str(request.url.copy_with(query=None)) for request in self.requests]


class _Raw:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, str]] = []

    def __call__(self, raw: bytes, symbol: str, *, payload_format: str = "json") -> RawObject:
        self.calls.append((raw, symbol, payload_format))
        digest = hashlib.sha256(raw).hexdigest()
        return RawObject(f"r2://{'a' * 32}/raw/hkex/{DATASET}/{digest}.htm.gz", digest, len(raw))


def _context(exchange: _Exchange, **changes: Any) -> tuple[AcquisitionContext, _Raw, list]:
    raw, observed = _Raw(), []
    values: dict[str, Any] = {
        "dataset_key": DATASET,
        "trade_date": DAY,
        "fetched_at": FETCHED,
        "feed": {"provider": "hkex", "dataset_key": DATASET},
        "delivery": {
            "slot_id": "asia_pacific_markets_window",
            "scheduled_for": FETCHED.isoformat(),
            "target_data_date": DAY.isoformat(),
            "work_item_id": "fp1:" + "a" * 32,
        },
        "contract": {"schema_id": "exchange_daily_quote", "current_version": 1},
        "limits": {"max_response_bytes": 64 * 1024 * 1024},
        "http": httpx.Client(transport=httpx.MockTransport(exchange)),
        "persist": raw,
        "observe_response": lambda size, declared=0: observed.append((size, declared)),
        "rate_limited": lambda: observed.append("rate_limited"),
        "snapshot": lambda url, provider_response=False: pytest.fail("no snapshot expected"),
    }
    values.update(changes)
    return AcquisitionContext(**values), raw, observed


def _member(symbol: str, classification: str = "Main Board") -> dict[str, Any]:
    return {
        "member_key": symbol,
        "symbol": symbol,
        "provider_symbol": symbol,
        "classification": classification,
        "mapping_status": "mapped",
    }


def test_parser_reads_quotes_and_halts_strictly() -> None:
    parsed = parse_report(MAIN, trade_date=DAY)

    assert not parsed.holiday
    assert parsed.rows["00700"] == {
        "symbol": "00700",
        "trade_date": "2026-11-20",
        "name": "TENCENT",
        "currency": "HKD",
        "trading_status": "traded",
        "previous_close": "348.40",
        "close": "344.00",
        "ask": "344.00",
        "bid": "343.80",
        "high": "349.60",
        "low": "339.60",
        "volume": 41094469,
        "turnover": "14089992865",
    }
    # The fixed-width name is read after entities are decoded.
    assert parsed.rows["00003"]["name"] == "HK & CHINA GAS"
    no_trade = parsed.rows["00025"]
    assert (no_trade["trading_status"], no_trade["volume"], no_trade["turnover"]) == (
        "no_trade",
        0,
        "0",
    )
    assert (no_trade["close"], no_trade["high"], no_trade["low"]) == ("4.40", None, None)
    assert parsed.halted == {
        "00036": ("TRADING SUSPENDED", "36 FE HLDGS INTL    HKD TRADING SUSPENDED"),
        "00690": ("TRADING HALTED", "690#UNI-BIO GROUP    HKD TRADING HALTED"),
    }
    assert parse_report(HOLIDAY, trade_date=DAY).holiday

    with pytest.raises(HkexReportFormatError, match="another date"):
        parse_report(_report(day="19 NOV 2026"), trade_date=DAY)
    unknown = MAIN.replace(b"TRADING HALTED", b"TRADING PAUSED")
    with pytest.raises(HkexReportFormatError, match="unknown line"):
        parse_report(unknown, trade_date=DAY)
    with pytest.raises(HkexReportFormatError, match="no quotations"):
        parse_report(b"<html>maintenance</html>", trade_date=DAY)


def test_only_a_range_of_the_whole_file_is_accepted() -> None:
    # The CDN appended its script to the first range response.
    exchange = _Exchange(main=[_respond(MAIN, extra=b"<script defer></script>"), _respond(MAIN)])
    observed: list[int] = []

    fetched = fetch_report(
        httpx.Client(transport=httpx.MockTransport(exchange)),
        board="main",
        trade_date=DAY,
        max_response_bytes=1024 * 1024,
        on_observed_bytes=observed.append,
    )

    assert gzip.decompress(fetched["gzip"]) == MAIN
    assert fetched["gzip"] == compress(MAIN)
    assert {key: fetched[key] for key in ("url", "original_bytes", "etag", "last_modified")} == {
        "url": MAIN_URL,
        "original_bytes": len(MAIN),
        "etag": '"0a7b6aee678dd1:0"',
        "last_modified": "2026-11-20T12:00:04+00:00",
    }
    assert fetched["original_sha256"] == hashlib.sha256(MAIN).hexdigest()
    first, second = exchange.requests
    assert first.headers["Range"] == "bytes=0-" and first.headers["Cache-Control"] == "no-cache"
    # Every request bypasses caches with its own query.
    assert first.url.query != second.url.query
    assert observed == [len(MAIN) + len(b"<script defer></script>"), len(MAIN)]


@pytest.mark.parametrize(
    ("response", "error", "retryable"),
    [
        # The server ignored the range: the body may carry the CDN's script.
        (_respond(MAIN, status=200), HkexOperationalError, True),
        (httpx.Response(404), HkexReportNotPublishedError, None),
        (httpx.Response(503), HkexOperationalError, True),
        (httpx.Response(403), HkexOperationalError, False),
    ],
)
def test_a_report_that_cannot_be_proven_is_never_returned(response, error, retryable) -> None:
    exchange = _Exchange(main=[response])

    with pytest.raises(error) as raised:
        fetch_report(
            httpx.Client(transport=httpx.MockTransport(exchange)),
            board="main",
            trade_date=DAY,
            max_response_bytes=1024 * 1024,
        )

    if retryable is not None:
        assert raised.value.retryable is retryable
    assert len(exchange.requests) == (
        hkex.REPORT_ATTEMPTS if retryable and response.status_code == 200 else 1
    )


def test_a_report_above_the_response_bound_is_not_retried() -> None:
    exchange = _Exchange(main=[_respond(MAIN)])

    with pytest.raises(HkexOperationalError) as raised:
        fetch_report(
            httpx.Client(transport=httpx.MockTransport(exchange)),
            board="main",
            trade_date=DAY,
            max_response_bytes=len(MAIN) - 1,
        )

    assert raised.value.retryable is False and len(exchange.requests) == 1


def _validated(request: dict[str, Any], stored: RawObject, contracts_dir: Path) -> dict[str, Any]:
    """The request as the runtime sends it: keyed and citing the raw object."""
    request = {**request, "request_key": "full:x", "idempotency_key": "full:x"}
    attach_raw_object(request, stored)
    ContractRegistry(contracts_dir).validate("exchange_daily_quote", 1, request)
    return request


def test_each_board_is_fetched_and_kept_once_for_every_member(contracts_dir) -> None:
    exchange = _Exchange(main=[_respond(MAIN)], gem=[_respond(GEM)])
    context, raw, _ = _context(exchange)
    adapter = HkexAdapter()

    tencent = adapter.acquire(_member("00700"), context)
    gas = adapter.acquire(_member("00003"), context)
    gem = adapter.acquire(_member("08003", "GEM"), context)

    assert exchange.urls() == [MAIN_URL, GEM_URL]
    assert [(symbol, kind) for _, symbol, kind in raw.calls] == [
        ("daily_quotation_main", "html_gzip"),
        ("daily_quotation_gem", "html_gzip"),
    ]
    assert raw.calls[0][0] == compress(MAIN)
    assert tencent.raw_object == gas.raw_object != gem.raw_object
    assert tencent.request is not None and gem.request is not None
    request = _validated(tencent.request, tencent.raw_object, contracts_dir)
    assert (
        request["payload"]["batch"]["source_raw_sha256"]
        == hashlib.sha256(compress(MAIN)).hexdigest()
    )
    assert request["payload"]["batch"]["report"] == {
        "board": "main",
        "url": MAIN_URL,
        "original_sha256": hashlib.sha256(MAIN).hexdigest(),
        "original_bytes": len(MAIN),
        "gzip_bytes": len(compress(MAIN)),
        "last_modified": "2026-11-20T12:00:04+00:00",
        "etag": '"0a7b6aee678dd1:0"',
    }
    assert [row["symbol"] for row in request["payload"]["data"]] == ["00700"]
    gem_request = _validated(gem.request, gem.raw_object, contracts_dir)
    assert gem_request["payload"]["batch"]["report"]["board"] == "gem"


def test_member_outcomes_follow_the_report(contracts_dir) -> None:
    exchange = _Exchange(main=[_respond(MAIN)], gem=[_respond(GEM)])
    context, _, _ = _context(exchange)
    adapter = HkexAdapter()

    quiet = adapter.acquire(_member("00025"), context)
    assert quiet.request is not None
    row = _validated(quiet.request, quiet.raw_object, contracts_dir)["payload"]["data"][0]
    assert (row["trading_status"], row["volume"]) == ("no_trade", 0)

    halted = adapter.acquire(_member("00690"), context)
    assert halted.request is None and halted.no_data_reason == "halted"
    assert halted.no_trade == {
        "url": MAIN_URL,
        "sha256": hashlib.sha256(compress(MAIN)).hexdigest(),
        "observed_at": FETCHED.isoformat(),
        "source_symbol": "00690",
        "trade_date": "2026-11-20",
        "source_status": "halted",
        "source_excerpt": "690#UNI-BIO GROUP    HKD TRADING HALTED",
    }
    assert halted.raw_object is not None

    # Listed, but in neither report: only this member is missing.
    with pytest.raises(MemberUnavailableError) as missing:
        adapter.acquire(_member("09999"), context)
    assert missing.value.absent and exchange.urls() == [MAIN_URL, GEM_URL]


def test_the_holiday_page_is_no_trade_evidence_for_the_requested_day() -> None:
    exchange = _Exchange(main=[_respond(HOLIDAY)])
    context, raw, _ = _context(exchange)

    result = HkexAdapter().acquire(_member("00700"), context)

    assert result.no_data_reason == "no_trade" and result.no_trade is not None
    assert result.no_trade["source_excerpt"] == HOLIDAY_EXCERPT
    assert result.no_trade["url"] == MAIN_URL and result.raw_object is not None
    assert len(raw.calls) == 1


def test_an_archived_report_is_imported_without_the_exchange(tmp_path, contracts_dir) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "d261120e.htm.gz").write_bytes(compress(MAIN))
    entry = {
        "file": "d261120e.htm",
        "etag": '"0a7b6aee678dd1:0"',
        "last_modified_hkt": "2026-11-20T20:00:04+08:00",
        "sha256": hashlib.sha256(MAIN).hexdigest(),
    }
    (tmp_path / "manifest.json").write_text(json.dumps([entry]))
    exchange = _Exchange(main=[httpx.Response(500)])
    context, _, _ = _context(exchange)

    result = HkexAdapter(tmp_path).acquire(_member("00700"), context)

    assert exchange.requests == []
    assert result.request is not None
    report = _validated(result.request, result.raw_object, contracts_dir)["payload"]["batch"]
    assert report["report"]["last_modified"] == "2026-11-20T12:00:04+00:00"
    # A file that is not the one the manifest recorded is refused.
    (tmp_path / "manifest.json").write_text(json.dumps([{**entry, "sha256": "0" * 64}]))
    with pytest.raises(HkexReportFormatError, match="differs from its manifest"):
        HkexAdapter(tmp_path).acquire(_member("00700"), context)


def test_the_exchange_list_maps_every_member_to_itself() -> None:
    listing = _hk_workbook("20/11/2026")
    context = UniverseContext(
        dataset_keys=[DATASET],
        observed_at=FETCHED,
        zones={DATASET: ZoneInfo("Asia/Hong_Kong")},
        snapshot=lambda url, provider_response=False: listing,
        catalogue=lambda **kwargs: pytest.fail("no provider catalogue"),
        catalogue_evidence=lambda url: pytest.fail("no provider catalogue"),
        persist=lambda *args, **kwargs: pytest.fail("evidence is persisted by the runtime"),
        observe_response=lambda *args, **kwargs: None,
        snapshot_objects={},
        refresh_limits=lambda: None,
        pace=lambda: None,
    )

    (snapshot,) = HkexAdapter().build_universe([], context).values()

    assert snapshot.effective_date == DAY and snapshot.evidence == {HKEX_URL: listing}
    assert [(m["symbol"], m["mapping_status"]) for m in snapshot.members] == [
        ("00700", "mapped"),
        ("83000", "mapped"),
    ]
    # The HKEX runtime keeps the list under its own dataset; FactSet's under its.
    assert official_snapshot_dataset(HKEX_URL, [DATASET]) == DATASET
    assert official_snapshot_dataset(HKEX_URL, ["hk_equity_eod"]) == "hk_equity_eod"


@pytest.mark.parametrize(
    ("exc", "kind", "wait"),
    [
        (HkexReportNotPublishedError("404"), FailureKind.ROUND_RETRY, "publication"),
        (HkexOperationalError("altered"), FailureKind.ROUND_RETRY, "outage"),
        (HkexOperationalError("503", status_code=503), FailureKind.ROUND_RETRY, "outage"),
        (httpx.ConnectError("down"), FailureKind.ROUND_RETRY, "outage"),
        (HkexOperationalError("403", status_code=403), FailureKind.WORK_FAILED, None),
        (HkexOperationalError("bound", retryable=False), FailureKind.WORK_FAILED, None),
        (HkexReportFormatError("format"), FailureKind.WORK_FAILED, None),
    ],
)
def test_report_failures_are_shared_by_the_whole_plan(exc, kind, wait) -> None:
    adapter = HkexAdapter()
    assert adapter.outage_holds_plan and adapter.shared_response_prefix == "hkex_report:"
    for phase in ("member", "round"):
        failure = adapter.classify(exc, phase)
        assert (failure.kind, failure.wait) == (kind, wait)
    assert adapter.classify(exc, "maintenance").kind == FailureKind.PASSTHROUGH


def test_a_restarted_attempt_replays_the_journaled_gzip(tmp_path) -> None:
    from types import SimpleNamespace

    from findb_fetcher import provider_work

    exchange = _Exchange(main=[_respond(MAIN)])
    client = httpx.Client(transport=httpx.MockTransport(exchange))
    path = tmp_path / "work.sqlite3"

    def owner() -> SimpleNamespace:
        journal = provider_work.WorkJournal(path, "work")
        return SimpleNamespace(journal=journal, provider="hkex", dispatch_observations=0)

    token = provider_work._current.set(owner())
    try:
        first = fetch_report(client, board="main", trade_date=DAY, max_response_bytes=1 << 20)
        provider_work._current.set(owner())
        replayed = fetch_report(client, board="main", trade_date=DAY, max_response_bytes=1 << 20)
        journal = provider_work.WorkJournal(path, "work")
        with journal.connection() as db:
            (body,) = db.execute("SELECT body FROM work_response").fetchone()
    finally:
        provider_work._current.reset(token)

    assert replayed == first and len(exchange.requests) == 1
    # The journal holds the compressed file only: the same bytes Raw R2 keeps.
    assert MAIN not in body and len(body) < len(MAIN) * 2
