"""Official exchange snapshots; classification and mapping never shrink coverage silently."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree as ET

NASDAQ_URLS = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)
HKEX_URL = (
    "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"
)
TW_COMPANY_URLS = (
    "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
)
TW_ISIN_URLS = tuple(
    f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}" for mode in (2, 4)
)
TAIFEX_URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"
TAIFEX_HISTORY_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


class UniverseSnapshotError(ValueError):
    """An official snapshot cannot be classified without losing evidence."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def checksum(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _member(
    symbol: str, exchange: str, currency: str, asset: str, classification: str
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "provider_symbol": symbol,
        "exchange": exchange,
        "currency": currency,
        "asset_class": asset,
        "classification": classification,
        "mapping_status": "gap",
    }


def nasdaq_effective_date(raw: bytes, *, observed_date: date) -> date:
    matched = re.search(rb"File Creation Time: ([0-9]{8})", raw)
    if matched is None:
        raise UniverseSnapshotError("Nasdaq snapshot timestamp is absent")
    day = datetime.strptime(matched.group(1).decode(), "%m%d%Y").date()
    if day > observed_date:
        raise UniverseSnapshotError("Nasdaq snapshot is future-dated")
    return day


def parse_nasdaq(raw: bytes, *, other: bool = False) -> list[dict[str, Any]]:
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    required = {"Security Name", "Test Issue", "ETF", "ACT Symbol" if other else "Symbol"}
    if not required.issubset(reader.fieldnames or []):
        raise UniverseSnapshotError("Nasdaq directory columns changed")
    exchanges = {"A": "XASE", "N": "XNYS", "P": "ARCX", "Z": "BATS", "V": "IEXG"}
    members = []
    for row in reader:
        symbol = row.get("ACT Symbol" if other else "Symbol", "")
        if symbol.startswith("File Creation Time"):
            continue
        name = row["Security Name"].lower()
        if row["Test Issue"] == "Y" or row["ETF"] == "Y" or "$" in symbol:
            continue
        if any(
            word in name
            for word in (
                "preferred",
                "preference",
                " pfd",
                "warrant",
                " rights",
                " units",
                " notes",
                " debenture",
                "depositary share representing",
                " closed-end",
                " etn",
            )
        ):
            continue
        exchange = exchanges.get(row.get("Exchange", "")) if other else "XNAS"
        if exchange is None:  # OTC/unknown markets are outside the requested major exchanges.
            continue
        if not re.fullmatch(r"[A-Za-z0-9.:-]{1,50}", symbol):
            raise UniverseSnapshotError("Nasdaq symbol needs an explicit canonical mapping")
        adr = any(term in name for term in ("american depositary", "american depository", " adr"))
        common = any(
            term in name
            for term in (
                "common stock",
                "common shares",
                "ordinary shares",
                "class a shares",
                "class b shares",
            )
        )
        classification = "adr" if adr else "ordinary" if common else "classification_gap"
        members.append(_member(symbol, exchange, "USD", "equity", classification))
    if not members:
        raise UniverseSnapshotError("Nasdaq directory is empty")
    return members


def _xlsx_rows(raw: bytes) -> list[dict[str, str]]:
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise UniverseSnapshotError("HKEX snapshot exceeds size limit")
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if (
                len(archive.infolist()) > 100
                or sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024
            ):
                raise UniverseSnapshotError("HKEX workbook expansion exceeds size limit")
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = [
                    "".join(node.itertext())
                    for node in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall(
                        "s:si", ns
                    )
                ]
            root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            result = []
            for row in root.findall(".//s:row", ns):
                cells = {}
                for cell in row.findall("s:c", ns):
                    value = cell.find("s:v", ns)
                    text = value.text or "" if value is not None else "".join(cell.itertext())
                    if cell.get("t") == "s":
                        text = strings[int(text)]
                    column = re.sub(r"[0-9]", "", cell.attrib["r"])
                    cells[column] = text.strip()
                result.append(cells)
            return result
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError, IndexError) as exc:
        raise UniverseSnapshotError("HKEX workbook shape is invalid") from exc


def parse_hkex(raw: bytes, *, observed_date: date) -> tuple[date, list[dict[str, Any]]]:
    rows = _xlsx_rows(raw)
    texts = " ".join(value for row in rows[:12] for value in row.values())
    numeric_date = re.search(r"(\d{2})/(\d{2})/(\d{4})", texts)
    matched = re.search(r"(\d{1,2})[ /-]([A-Za-z]{3,9})[ /-](\d{4})", texts)
    if matched is None and numeric_date is None:
        raise UniverseSnapshotError("HKEX snapshot publication date is absent")
    if numeric_date is not None:
        snapshot_date = datetime.strptime("/".join(numeric_date.groups()), "%d/%m/%Y").date()
    for fmt in () if numeric_date is not None else ("%d %b %Y", "%d %B %Y"):
        try:
            assert matched is not None
            snapshot_date = datetime.strptime(" ".join(matched.groups()), fmt).date()
            break
        except ValueError:
            pass
    else:
        if numeric_date is None:
            raise UniverseSnapshotError("HKEX publication date is invalid")
    if snapshot_date > observed_date:
        raise UniverseSnapshotError("HKEX snapshot is future-dated")
    members = []
    for row in rows:
        code = row.get("A", "")
        if row.get("C") != "Equity":
            continue
        if not code.isdigit() or len(code) > 5 or row.get("Q") not in {"HKD", "CNY", "RMB", "USD"}:
            raise UniverseSnapshotError("HKEX equity identity or currency is invalid")
        name = row.get("B", "").lower()
        if "preference" in name or "preferred" in name:
            continue
        members.append(
            _member(
                code.zfill(5),
                "XHKG",
                "CNY" if row["Q"] == "RMB" else row["Q"],
                "equity",
                row["D"]
                if row.get("D")
                in {
                    "Main Board",
                    "GEM",
                    "Equity Securities (Main Board)",
                    "Equity Securities (GEM)",
                }
                else "classification_gap",
            )
        )
    if not members:
        raise UniverseSnapshotError("HKEX equity snapshot is empty")
    return snapshot_date, members


class _ISINParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)


def parse_tw_companies(raw: bytes, *, otc: bool = False) -> list[dict[str, Any]]:
    rows = json.loads(raw)
    if not isinstance(rows, list) or not rows:
        raise UniverseSnapshotError("Taiwan company snapshot is empty")
    members = []
    for row in rows:
        symbol = str(row.get("SecuritiesCompanyCode" if otc else "公司代號", "")).strip()
        if not otc and re.fullmatch(r"91[0-9]{4}", symbol):
            continue  # The official company table also carries TDRs, outside ordinary-share scope.
        if not re.fullmatch(r"[0-9]{4}", symbol):
            raise UniverseSnapshotError("Taiwan ordinary company identity is invalid")
        members.append(_member(symbol, "ROCO" if otc else "XTAI", "TWD", "equity", "ordinary"))
    return members


def parse_tw_ordinary(raw: bytes, *, otc: bool = False) -> list[dict[str, Any]]:
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise UniverseSnapshotError("ISIN snapshot exceeds size limit")
    parser = _ISINParser()
    parser.feed(raw.decode("cp950"))
    in_stock = False
    members = []
    for row in parser.rows:
        if len(row) == 1:
            in_stock = row[0] == "股票"
            continue
        if not in_stock or len(row) < 6:
            continue
        # CFI ES is an equity share. Depositary receipts ED and preferred EP are excluded,
        # including four-digit TDR codes; code length alone is not classification evidence.
        cfi = row[5].strip()
        if cfi.startswith(("ED", "EP")):
            continue
        match = re.match(r"^([0-9]{4,6}[A-Z]?)\s+", row[0].replace("\u3000", " "))
        if match is None:
            raise UniverseSnapshotError("Taiwan stock identity is invalid")
        member = _member(
            match.group(1),
            "ROCO" if otc else "XTAI",
            "TWD",
            "equity",
            "ordinary" if cfi.startswith("ES") else "classification_gap",
        )
        members.append(member)
    if not members:
        raise UniverseSnapshotError("Taiwan ordinary stock section is empty or changed")
    return members


def parse_tw_etfs(raw: bytes, *, otc: bool = False) -> list[dict[str, Any]]:
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise UniverseSnapshotError("ISIN snapshot exceeds size limit")
    parser = _ISINParser()
    parser.feed(raw.decode("cp950"))
    in_etf = False
    members = []
    for row in parser.rows:
        if len(row) == 1:
            # Exact ETF section includes active, leveraged/inverse and bond ETFs.
            in_etf = row[0] in {
                "ETF",
                "ETF（指數股票型基金）",
                "指數股票型基金(ETF)",
                "指數股票型基金（ETF）",
            }
            continue
        if not in_etf or len(row) < 6:
            continue
        match = re.match(r"^([0-9]{4,6}[A-Z]?)\s+", row[0].replace("\u3000", " "))
        if match is None:
            raise UniverseSnapshotError("Taiwan ETF identity is invalid")
        members.append(_member(match.group(1), "ROCO" if otc else "XTAI", "TWD", "etf", "ETF"))
    if not members:
        raise UniverseSnapshotError("Taiwan ETF section is empty or changed")
    return members


def map_catalogue(
    members: Iterable[dict[str, Any]],
    catalogue: Iterable[Mapping[str, Any]],
    *,
    shioaji: bool = False,
) -> list[dict[str, Any]]:
    """Retain every expected member; only exact exchange/currency mappings become mapped."""
    indexed: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in catalogue:
        symbol = str(row.get("symbol", ""))
        exchange = str(row.get("mic_code", row.get("exchange", "")))
        currency = str(row.get("currency", ""))
        if shioaji:
            exchange = {"TSE": "XTAI", "OTC": "ROCO"}.get(exchange, exchange)
            currency = currency or "TWD"
        indexed.setdefault((symbol, exchange, currency), []).append(row)
    result = []
    for member in members:
        member = dict(member)
        # HKEX numeric canonical code keeps zeros; provider catalogue is joined by numeric code.
        candidates = indexed.get((member["symbol"], member["exchange"], member["currency"]), [])
        if member["exchange"] == "XHKG" and not candidates:
            candidates = [
                row
                for (symbol, exchange, currency), rows in indexed.items()
                if symbol.isdigit()
                and symbol.zfill(5) == member["symbol"]
                and exchange == "XHKG"
                and currency == member["currency"]
                for row in rows
            ]
        if len(candidates) == 1 and member["classification"] != "classification_gap":
            row = candidates[0]
            if shioaji or row.get("type") in {
                "Common Stock",
                "American Depositary Receipt",
                "Depositary Receipt",
            }:
                member["provider_symbol"] = str(row.get("provider_symbol", row["symbol"]))
                member["mapping_status"] = "mapped"
        result.append(member)
    return result


def universe_request(
    *,
    dataset_key: str,
    provider: str,
    effective_date: date,
    observed_at: datetime,
    source_timezone: str,
    snapshots: Mapping[str, bytes],
    members: list[dict[str, Any]],
) -> dict[str, Any]:
    if observed_at.tzinfo is None or effective_date > observed_at.date() or not members:
        raise UniverseSnapshotError("universe date, clock or members are invalid")
    ordered = sorted(
        members, key=lambda member: (member["symbol"], member.get("contract_code", ""))
    )
    return {
        "version": 1,
        "dataset_key": dataset_key,
        "provider": provider,
        "effective_date": effective_date.isoformat(),
        "observed_at": observed_at.isoformat(),
        "source_timezone": source_timezone,
        "evidence": [
            {"url": url, "sha256": checksum(raw)} for url, raw in sorted(snapshots.items())
        ],
        "members": ordered,
    }
