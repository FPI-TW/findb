"""
Backfill ``name`` and ``currency`` on the ``instruments`` table for the
non-TW markets (US, HK, CN, FX, plus indices on every market).

Sources (all free, no API key required):

- **US equity** – NASDAQ Trader symbol directory
  (``nasdaqlisted.txt`` + ``otherlisted.txt``). Pipe-delimited, ASCII,
  English company names.
- **HK equity** – HKEX 證券名單 (``ListOfSecurities_c.xlsx``).
  Traditional Chinese names.
- **CN equity** – Tencent 行情 quote endpoint
  (``https://qt.gtimg.cn/q=sh600000,sz000001…``). Simplified Chinese
  short names. Batched (default 60 symbols per request).
- **FX** – static dict in this file. Most ``FX/fx`` rows are
  Bloomberg-style macro/derivative tickers, not pure FX pairs, so a
  hand-curated map is the only reliable path.
- **Indices** – static dict in this file. Indices have small, fixed
  inventories per market (HSI family, S&P 500 sectors, 上證 行業 etc.).

The script is idempotent: every column is only filled where it is
currently ``NULL``. Re-running after a partial update only writes the
fields that are still empty.

Run with ``--dry-run`` (default) to preview, ``--apply`` to write.
Run with ``--markets us,hk`` to limit scope; default is ``all``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import ssl
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.canonical import Instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

US_NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
US_OTHER_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
HK_HKEX_URL = (
    "https://www.hkex.com.hk/chi/services/trading/securities/securitieslists/"
    "ListOfSecurities_c.xlsx"
)
CN_TENCENT_URL_FMT = "https://qt.gtimg.cn/q={codes}"
CN_BATCH_SIZE = 60


# ---------------------------------------------------------------------------
# Static dictionaries
# ---------------------------------------------------------------------------

FX_NAMES: dict[str, str] = {
    "AUDJPY": "澳幣兌日圓",
    "AUDUSD": "澳幣兌美元",
    "BRLJPY": "巴西里拉兌日圓",
    "CESIEU": "花旗歐元區經濟驚奇指數",
    "CESIUS": "花旗美國經濟驚奇指數",
    "CNH": "離岸人民幣",
    "CNY": "在岸人民幣",
    "DXY": "美元指數",
    "EURUSD": "歐元兌美元",
    "EURUSD25R2M": "歐元/美元 2 個月 25 Delta 風險逆轉",
    "GBPUSD": "英鎊兌美元",
    "GDBR": "德國公債殖利率",
    "GTJPY": "日本公債殖利率",
    "IMM": "IMM 期貨倉位",
    "IMMBEN": "IMM 商業避險倉位",
    "JPMVXY": "摩根 G7 外匯波動率指數",
    "JPYAUD": "日圓兌澳幣",
    "JPYBRL": "日圓兌巴西里拉",
    "TWD": "新台幣",
    "USDCAD": "美元兌加幣",
    "USDCHF": "美元兌瑞郎",
    "USDCNH": "美元兌離岸人民幣",
    "USDJPY": "美元兌日圓",
    "USDJPY25R3M": "美元/日圓 3 個月 25 Delta 風險逆轉",
    "USDKRW": "美元兌韓元",
    "USDSEK": "美元兌瑞典克朗",
    "USDSGD": "美元兌新加坡元",
    "USDTWD": "美元兌新台幣",
    "USGG": "美國公債殖利率",
    "USGG10YR": "美國 10 年期公債殖利率",
    "VIX": "VIX 波動率指數",
}

# (market, symbol) -> name. Currency is derived from the market in
# ``run_indices`` (US→USD, HK→HKD, CN→CNY, TW→TWD).
INDEX_NAMES: dict[tuple[str, str], str] = {
    # --- US ---
    ("US", "SPX"): "S&P 500 指數",
    ("US", "NDX"): "NASDAQ 100 指數",
    ("US", "INDU"): "道瓊工業平均指數",
    ("US", "CCMP"): "NASDAQ 綜合指數",
    ("US", "RTY"): "羅素 2000 指數",
    ("US", "SOX"): "費城半導體指數",
    ("US", "VIX"): "CBOE 波動率指數",
    ("US", "S5COND"): "S&P 500 非必需消費品行業指數",
    ("US", "S5CONS"): "S&P 500 必需消費品行業指數",
    ("US", "S5ENRS"): "S&P 500 能源行業指數",
    ("US", "S5FINL"): "S&P 500 金融行業指數",
    ("US", "S5HLTH"): "S&P 500 醫療保健行業指數",
    ("US", "S5INDU"): "S&P 500 工業行業指數",
    ("US", "S5INFT"): "S&P 500 資訊科技行業指數",
    ("US", "S5MATR"): "S&P 500 原材料行業指數",
    ("US", "S5RLST"): "S&P 500 房地產行業指數",
    ("US", "S5TELS"): "S&P 500 通訊服務行業指數",
    ("US", "S5UTIL"): "S&P 500 公用事業行業指數",
    # BM7T is intentionally omitted — meaning is ambiguous in our datasource;
    # leaving it unmatched so the operator can fill it in manually.
    # --- HK ---
    ("HK", "HSI"): "恒生指數",
    ("HK", "HSCEI"): "恒生中國企業指數",
    ("HK", "HSTECH"): "恒生科技指數",
    ("HK", "HSMSI"): "恒生中小型股指數",
    ("HK", "VHSI"): "恒生波幅指數",
    ("HK", "HSCICD"): "恒生綜合行業指數 - 非必需性消費",
    ("HK", "HSCICS"): "恒生綜合行業指數 - 必需性消費",
    ("HK", "HSCIEN"): "恒生綜合行業指數 - 能源",
    ("HK", "HSCIFN"): "恒生綜合行業指數 - 金融",
    ("HK", "HSCIH"): "恒生綜合行業指數 - 醫療保健",
    ("HK", "HSCIIN"): "恒生綜合行業指數 - 工業",
    ("HK", "HSCIIT"): "恒生綜合行業指數 - 資訊科技",
    ("HK", "HSCIMT"): "恒生綜合行業指數 - 原材料",
    ("HK", "HSCIPC"): "恒生綜合行業指數 - 地產建築",
    ("HK", "HSCITC"): "恒生綜合行業指數 - 電訊",
    ("HK", "HSCIUT"): "恒生綜合行業指數 - 公用事業",
    # --- CN ---
    ("CN", "SHCOMP"): "上證綜合指數",
    ("CN", "SHSZ300"): "滬深 300 指數",
    ("CN", "SZ399006"): "創業板指數",
    ("CN", "SZCOMP"): "深證成份指數",
    ("CN", "SH000908"): "上證能源行業指數",
    ("CN", "SH000909"): "上證原材料行業指數",
    ("CN", "SH000910"): "上證工業行業指數",
    ("CN", "SH000911"): "上證非必需消費行業指數",
    ("CN", "SH000912"): "上證主要消費行業指數",
    ("CN", "SH000913"): "上證醫療保健行業指數",
    ("CN", "SH000914"): "上證金融地產行業指數",
    ("CN", "SH000915"): "上證資訊技術行業指數",
    ("CN", "SH000916"): "上證電信服務行業指數",
    ("CN", "SH000917"): "上證公用事業行業指數",
}

INDEX_CURRENCY_BY_MARKET: dict[str, str] = {
    "US": "USD",
    "HK": "HKD",
    "CN": "CNY",
    "TW": "TWD",
}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


_UNVERIFIED_HOSTS = frozenset({"isin.twse.com.tw"})


def _curl(url: str, *, timeout: int = 300) -> bytes:
    """Fetch ``url`` via stdlib ``urllib``. Used so the script runs inside
    the slim runtime container without depending on a ``curl`` binary.

    NASDAQ Trader, HKEX, and Tencent all serve valid certs and go through
    the default verifier. The TWSE ISIN registry's cert is missing a
    Subject Key Identifier and Python rejects it; since the page is fully
    public and read-only, we use an unverified context for that host only.
    """
    host_match = re.match(r"^https?://([^/]+)", url)
    host = host_match.group(1).lower() if host_match else ""
    if host in _UNVERIFIED_HOSTS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx = ssl.create_default_context()
    req = Request(url, headers={"User-Agent": "findb-backfill/1.0"})
    with urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 (fixed URLs)
        return resp.read()


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


_US_NAME_SUFFIXES = (
    " - Common Stock",
    " - American Depositary Shares",
    " - Class A Common Stock",
    " - Class A Ordinary Shares",
    " - Ordinary Shares",
    " Common Stock",
    " Common Shares",
    " American Depositary Shares",
    " Ordinary Shares",
)


def _clean_us_name(name: str) -> str:
    """Strip common boilerplate suffixes from NASDAQ Trader security names."""
    for suffix in _US_NAME_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)].strip()
    # If the security name contains "- <descriptor>", keep just the issuer
    # half to match how the rest of our names are stored ("ABC Inc." rather
    # than "ABC Inc. - Class A Common Stock").
    if " - " in name:
        return name.split(" - ", 1)[0].strip()
    return name.strip()


def fetch_us_equity_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for url in (US_NASDAQ_URL, US_OTHER_URL):
        text = _curl(url).decode("utf-8", errors="replace")
        lines = text.splitlines()
        if not lines:
            continue
        for line in lines[1:]:
            if not line or line.startswith("File Creation Time:"):
                continue
            parts = line.split("|")
            if len(parts) < 2:
                continue
            sym = parts[0].strip()
            name = parts[1].strip()
            if not sym or not name:
                continue
            cleaned = _clean_us_name(name)
            out.setdefault(sym, cleaned)
            # NASDAQ Trader uses "BRK.B" / "BF.B" notation for class-share
            # tickers; FinDB stores these with a slash ("BRK/B", "BF/B").
            # Register both spellings so the matcher hits either way.
            if "." in sym:
                out.setdefault(sym.replace(".", "/"), cleaned)
    return out


def fetch_hk_equity_map() -> dict[str, str]:
    """Return ``{symbol_no_leading_zero: traditional_chinese_name}``.

    Parses the HKEX ``ListOfSecurities_c.xlsx`` using only the standard
    library (``zipfile`` + ``xml.etree``) so the script runs inside the
    production container without extra deps.
    """
    data = _curl(HK_HKEX_URL)
    out: dict[str, str] = {}
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(BytesIO(data)) as zf:
        sheet_names = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet")]
        if not sheet_names:
            return out
        with zf.open("xl/sharedStrings.xml") as f:
            shared_strings: list[str] = []
            for _, el in ET.iterparse(f, events=("end",)):
                if el.tag == f"{ns}si":
                    shared_strings.append("".join((t.text or "") for t in el.iter(f"{ns}t")))
                    el.clear()
        with zf.open(sorted(sheet_names)[0]) as f:
            current: dict[str, str] = {}
            for _, el in ET.iterparse(f, events=("end",)):
                tag = el.tag
                if tag == f"{ns}c":
                    ref = el.get("r", "")
                    col = "".join(ch for ch in ref if ch.isalpha())
                    if col not in ("A", "B"):
                        el.clear()
                        continue
                    v = el.find(f"{ns}v")
                    if v is None or v.text is None:
                        el.clear()
                        continue
                    value = v.text
                    if el.get("t") == "s":
                        value = shared_strings[int(value)]
                    current[col] = value
                    el.clear()
                elif tag == f"{ns}row":
                    sym_padded = current.get("A", "").strip()
                    name = current.get("B", "").strip()
                    if sym_padded.isdigit() and name:
                        sym = sym_padded.lstrip("0") or "0"
                        out.setdefault(sym, name)
                    current = {}
                    el.clear()
    return out


_TENCENT_RE = re.compile(r'v_(sh|sz)(\d+)="([^"]*)"')


def fetch_cn_equity_map(symbols: list[str]) -> dict[str, str]:
    """Query Tencent for the given 6-digit CN symbols. Returns
    ``{symbol: simplified_chinese_short_name}``.
    """
    out: dict[str, str] = {}
    queryable: list[tuple[str, str]] = []  # (db_symbol, tencent_code)
    for sym in symbols:
        sym = sym.strip()
        if not sym.isdigit() or len(sym) != 6:
            continue
        # 6xxxxx — SH A-shares; 9xxxxx — SH B-shares.
        # 0xxxxx / 3xxxxx — SZ main board / ChiNext; 2xxxxx — SZ B-shares.
        if sym.startswith(("6", "9")):
            queryable.append((sym, f"sh{sym}"))
        elif sym.startswith(("0", "2", "3")):
            queryable.append((sym, f"sz{sym}"))
    for i in range(0, len(queryable), CN_BATCH_SIZE):
        batch = queryable[i : i + CN_BATCH_SIZE]
        codes = ",".join(code for _, code in batch)
        url = CN_TENCENT_URL_FMT.format(codes=codes)
        text = _curl(url).decode("gbk", errors="replace")
        for line in text.splitlines():
            m = _TENCENT_RE.match(line)
            if not m:
                continue
            _, code, payload = m.groups()
            parts = payload.split("~")
            if len(parts) >= 2 and parts[1]:
                out[code] = parts[1].strip()
    return out


# ---------------------------------------------------------------------------
# DB updater
# ---------------------------------------------------------------------------


@dataclass
class UpdateResult:
    label: str
    scanned: int = 0
    name_updates: int = 0
    currency_updates: int = 0
    unmatched: list[str] = field(default_factory=list)


async def _apply_name_map(
    session: AsyncSession,
    *,
    label: str,
    market: str,
    asset_class: str,
    name_map: dict[str, str],
    default_currency: str | None,
    apply: bool,
) -> UpdateResult:
    stmt = select(Instrument).where(
        Instrument.market == market,
        Instrument.asset_class == asset_class,
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    r = UpdateResult(label=label, scanned=len(rows))
    for inst in rows:
        if inst.name is None:
            name = name_map.get(inst.symbol)
            if name:
                if apply:
                    inst.name = name
                r.name_updates += 1
            else:
                r.unmatched.append(inst.symbol)
        if default_currency and inst.currency is None:
            if apply:
                inst.currency = default_currency
            r.currency_updates += 1
    return r


async def run_us(session: AsyncSession, *, apply: bool) -> UpdateResult:
    name_map = fetch_us_equity_map()
    logger.info("US: fetched %d entries from NASDAQ Trader", len(name_map))
    return await _apply_name_map(
        session,
        label="US/equity",
        market="US",
        asset_class="equity",
        name_map=name_map,
        default_currency="USD",
        apply=apply,
    )


async def run_hk(session: AsyncSession, *, apply: bool) -> UpdateResult:
    name_map = fetch_hk_equity_map()
    logger.info("HK: fetched %d entries from HKEX ListOfSecurities", len(name_map))
    return await _apply_name_map(
        session,
        label="HK/equity",
        market="HK",
        asset_class="equity",
        name_map=name_map,
        default_currency="HKD",
        apply=apply,
    )


async def run_cn(session: AsyncSession, *, apply: bool) -> UpdateResult:
    stmt = select(Instrument.symbol).where(
        Instrument.market == "CN",
        Instrument.asset_class == "equity",
    )
    result = await session.execute(stmt)
    db_symbols = sorted({s for s in result.scalars().all() if s})
    name_map = fetch_cn_equity_map(db_symbols)
    logger.info(
        "CN: fetched %d entries from Tencent for %d DB symbols", len(name_map), len(db_symbols)
    )
    return await _apply_name_map(
        session,
        label="CN/equity",
        market="CN",
        asset_class="equity",
        name_map=name_map,
        default_currency="CNY",
        apply=apply,
    )


async def run_fx(session: AsyncSession, *, apply: bool) -> UpdateResult:
    return await _apply_name_map(
        session,
        label="FX/fx",
        market="FX",
        asset_class="fx",
        name_map=FX_NAMES,
        default_currency=None,
        apply=apply,
    )


async def run_indices(session: AsyncSession, *, apply: bool) -> UpdateResult:
    stmt = select(Instrument).where(Instrument.asset_class == "index")
    result = await session.execute(stmt)
    rows = result.scalars().all()
    r = UpdateResult(label="indices", scanned=len(rows))
    for inst in rows:
        key = (inst.market, inst.symbol)
        if inst.name is None:
            name = INDEX_NAMES.get(key)
            if name:
                if apply:
                    inst.name = name
                r.name_updates += 1
            else:
                r.unmatched.append(f"{inst.market}/{inst.symbol}")
        if inst.currency is None:
            currency = INDEX_CURRENCY_BY_MARKET.get(inst.market)
            if currency:
                if apply:
                    inst.currency = currency
                r.currency_updates += 1
    return r


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


MARKET_RUNNERS = {
    "us": run_us,
    "hk": run_hk,
    "cn": run_cn,
    "fx": run_fx,
    "indices": run_indices,
}
ALL_MARKETS = list(MARKET_RUNNERS.keys())


def _log_result(r: UpdateResult) -> None:
    logger.info(
        "%-12s scanned=%-5d name+=%-5d currency+=%-5d unmatched=%d",
        r.label,
        r.scanned,
        r.name_updates,
        r.currency_updates,
        len(r.unmatched),
    )
    sample = r.unmatched[:10]
    if sample:
        logger.warning("  unmatched sample: %s%s", sample, " ..." if len(r.unmatched) > 10 else "")


async def run_all(markets: list[str], apply: bool) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_maker() as session:
            for market in markets:
                runner = MARKET_RUNNERS[market]
                logger.info("=== %s ===", market.upper())
                r = await runner(session, apply=apply)
                _log_result(r)
            if apply:
                await session.commit()
                logger.info("committed all updates")
            else:
                logger.info("dry-run: no changes committed")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--markets",
        default="all",
        help=f"Comma-separated subset of: {','.join(ALL_MARKETS)}. Default 'all'.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates. Without this flag the script runs as a dry run.",
    )
    args = parser.parse_args()

    if args.markets == "all":
        markets = list(ALL_MARKETS)
    else:
        markets = [m.strip().lower() for m in args.markets.split(",") if m.strip()]
        unknown = [m for m in markets if m not in MARKET_RUNNERS]
        if unknown:
            parser.error(f"unknown markets: {unknown}; valid: {ALL_MARKETS}")

    asyncio.run(run_all(markets, args.apply))


if __name__ == "__main__":
    main()
