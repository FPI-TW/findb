"""Golden universe refresh outputs; moving universe building must stay byte-identical.

Each provider refreshes its universe from fixed official snapshots and native
catalogues under a fixed clock. The exact Source universe bodies and the
distinct R2 objects are compared with ``fixtures/provider_universe_golden.json``.
Fakes sit at transport or class level so they survive module moves.

Regenerate only for an intended contract change: ``FINDB_UPDATE_GOLDEN=1 pytest``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from findb_fetcher.full_market_universe import (
    HKEX_URL,
    NASDAQ_URLS,
    TAIFEX_URL,
    TW_COMPANY_URLS,
    TW_ISIN_URLS,
    canonical_bytes,
)
from test_provider_adapter_golden import _clock, _runtime

GOLDEN = Path(__file__).parent / "fixtures/provider_universe_golden.json"
FIXED = datetime(2026, 10, 2, 1, 30, tzinfo=timezone.utc)


def _check(name: str, actual: dict[str, Any]) -> None:
    actual = json.loads(json.dumps(actual, sort_keys=True, default=str))
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if os.getenv("FINDB_UPDATE_GOLDEN") == "1":
        golden[name] = actual
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    assert actual == golden[name]


def _refresh(tmp_path, monkeypatch, provider, official, dataset_keys):
    """Run one universe refresh against fixed official responses."""
    from execution_config_support import publication

    _clock(monkeypatch)
    requested: list[str] = []

    def native(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        for prefix, body in official.items():
            if url == prefix or url.startswith(prefix + "?"):
                return httpx.Response(200, content=body)
        return httpx.Response(404)

    runtime, source, _, s3 = _runtime(
        tmp_path, provider, httpx.Client(transport=httpx.MockTransport(native))
    )
    runtime._ensure_proof = lambda: None
    source.execution_config = lambda: publication(provider)
    submitted: list[dict[str, Any]] = []
    source.submit = lambda body: submitted.append(body) or {"status": "published"}
    try:
        runtime.sync_universes(dataset_keys=dataset_keys)
    finally:
        runtime.close()
    objects: list[dict[str, Any]] = []
    for put in s3.puts:
        entry = {
            "key": put["Key"],
            "content_type": put["ContentType"],
            "metadata": put["Metadata"],
            "sha256": hashlib.sha256(put["Body"]).hexdigest(),
        }
        if entry not in objects:
            objects.append(entry)
    return {
        "requested": requested,
        "submitted": [canonical_bytes(body).decode() for body in submitted],
        "raw_objects": objects,
    }


def test_twelve_data_universe_refresh_is_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "golden-key")
    listed = (
        b"Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        b"AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N\n"
        b"QQQ|Invesco QQQ Trust|G|N|N|100|Y|N\n"
        b"File Creation Time: 1001202621:31|||||||\n"
    )
    other = (
        b"ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        b"IBM|International Business Machines Corporation Common Stock|N|IBM|N|100|N|IBM\n"
        b"NEW|Brand New Corp Common Stock|N|NEW|N|100|N|NEW\n"
        b"File Creation Time: 1001202621:31|||||||\n"
    )
    stocks = canonical_bytes(
        {
            "data": [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc.",
                    "currency": "USD",
                    "mic_code": "XNGS",
                    "type": "Common Stock",
                },
                {
                    "symbol": "IBM",
                    "name": "International Business Machines Corporation",
                    "currency": "USD",
                    "mic_code": "XNYS",
                    "type": "Common Stock",
                },
            ]
        }
    )
    official = {
        NASDAQ_URLS[0]: listed,
        NASDAQ_URLS[1]: other,
        "https://api.twelvedata.com/stocks": stocks,
    }
    _check(
        "twelve_data",
        _refresh(tmp_path, monkeypatch, "twelve_data", official, ["us_equity_eod"]),
    )


def _hk_workbook(day: str) -> bytes:
    rows = [f'<row><c r="A2" t="inlineStr"><is><t>Updated as at{day}</t></is></c></row>']
    for number, code, currency, category, board in (
        (4, "1", "HKD", "Equity", "Main Board"),
        (5, "700", "HKD", "Equity", "Main Board"),
        (6, "83000", "CNY", "Equity", "GEM"),
        (7, "2800", "HKD", "Exchange Traded Products", "Main Board"),
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
        # A fixed member timestamp keeps the workbook bytes deterministic.
        archive.writestr(zipfile.ZipInfo("xl/worksheets/sheet1.xml", (2026, 10, 2, 9, 0, 0)), sheet)
    return data.getvalue()


def test_factset_universe_refresh_is_byte_identical(tmp_path, monkeypatch):
    from findb_fetcher.providers.factset import FactSetConfig, FactSetGateway

    monkeypatch.setattr(FactSetConfig, "from_env", classmethod(lambda cls: "golden-config"))
    monkeypatch.setattr(FactSetGateway, "__init__", lambda self, config: None)
    queried: list[tuple[str, ...]] = []

    def catalogue(self, symbols):
        queried.append(tuple(symbols))
        rows = [
            {
                "ticker_exchange": "1-HKG",
                "fsym_id": "AAAAA1-L",
                "listing_flag": 1,
                "fref_security_type": "SHARE",
                "currency": "HKD",
            },
            {
                "ticker_exchange": "700-HKG",
                "fsym_id": "AAAAA7-L",
                "listing_flag": 1,
                "fref_security_type": "SHARE",
                "currency": "USD",
            },
        ]
        return canonical_bytes({"rows": rows, "scope": {"symbols": list(symbols)}})

    monkeypatch.setattr(FactSetGateway, "catalogue", catalogue)
    result = _refresh(
        tmp_path, monkeypatch, "factset", {HKEX_URL: _hk_workbook("02/10/2026")}, ["hk_equity_eod"]
    )
    _check("factset", {"queried": queried, **result})


def _isin(rows: list[tuple[str, str, str]], *, etf: bool = False) -> bytes:
    html = "<table><tr><td>股票</td></tr>"
    for symbol, cfi, listed in rows:
        if symbol == "ETF":
            html += "<tr><td>ETF</td></tr>"
            continue
        html += (
            f"<tr><td>{symbol}　名稱</td><td>TW000{symbol}</td><td>{listed}</td>"
            f"<td>上市</td><td>產業</td><td>{cfi}</td><td></td></tr>"
        )
    return (html + "</table>").encode("cp950")


@pytest.mark.parametrize("dataset_keys", [["tw_equity_eod"], ["tw_etf_eod"]], ids=["equity", "etf"])
def test_finlab_universe_refresh_is_byte_identical(tmp_path, monkeypatch, dataset_keys):
    import importlib.metadata

    from findb_fetcher.providers.finlab import FinLabDatasetTable, FinLabSdkGateway

    real_version = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: "1.5.7" if name == "finlab" else real_version(name),
    )
    observed: list[tuple[str, ...]] = []

    class Gateway:
        def fetch_catalogue(self, *, symbols):
            observed.append(tuple(symbols))
            served = tuple(symbol for symbol in symbols if symbol not in {"1101", "00632R"})
            return FinLabDatasetTable(("2026-10-01",), served, (tuple("1" for _ in served),))

    monkeypatch.setattr(FinLabSdkGateway, "from_env", classmethod(lambda cls, **kwargs: Gateway()))
    official = {
        TW_COMPANY_URLS[0]: '[{"公司代號":"2330"},{"公司代號":"1101"}]'.encode(),
        TW_COMPANY_URLS[1]: b'[{"SecuritiesCompanyCode":"6488"}]',
        TW_ISIN_URLS[0]: _isin(
            [
                ("2330", "ESVUFR", "1994/09/05"),
                ("1101", "ESVUFR", "1962/02/09"),
                ("2317", "ESVUFR", "1991/06/18"),
                ("ETF", "", ""),
                ("0050", "CEOGEU", "2003/06/30"),
            ]
        ),
        TW_ISIN_URLS[1]: _isin(
            [
                ("6488", "ESVUFR", "2014/03/26"),
                ("ETF", "", ""),
                ("00632R", "CEOGEU", "2014/10/31"),
            ]
        ),
    }
    result = _refresh(tmp_path, monkeypatch, "finlab", official, dataset_keys)
    _check(f"finlab:{dataset_keys[0]}", {"observed": observed, **result})


def test_taifex_universe_refresh_is_byte_identical(tmp_path, monkeypatch):
    report = canonical_bytes(
        [
            {
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
            for product, month in (("TX", "202610"), ("MTX", "202610W2"))
            for session in ("一般", "盤後")
        ]
    )
    _check(
        "taifex",
        _refresh(tmp_path, monkeypatch, "taifex", {TAIFEX_URL: report}, ["tw_futures_eod"]),
    )
