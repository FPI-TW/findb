"""
Backfill TW instrument ``name`` and ``currency`` columns from the TWSE/TPEX
public ISIN registry.

Source pages (Big5 HTML):

- https://isin.twse.com.tw/isin/C_public.jsp?strMode=2  (上市 TWSE)
- https://isin.twse.com.tw/isin/C_public.jsp?strMode=4  (上櫃 TPEX)

Each row carries: ``<code>　<name>``, ISIN, listing date, market category,
industry, CFICode, remark. CFICode prefix maps to our ``asset_class``:

- ``E*`` → equity
- ``C*`` → etf (collective investment vehicle)

Behavior:

- Only updates rows where the target field is currently ``NULL`` — re-running
  the script is a no-op once names are filled.
- Matches DB rows by ``(asset_class, market='TW', symbol)``. CFICode in the
  ISIN registry decides asset_class.
- ``currency`` is set to ``TWD`` for every matched TW row.

Run with ``--dry-run`` (default) to preview, ``--apply`` to write.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.canonical import Instrument

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TWSE_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
TPEX_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"

# CFICode first character → our asset_class label. Other prefixes (D=debt,
# O=options, M=misc) are ignored because we don't store them as TW instruments.
ASSET_CLASS_BY_CFI: dict[str, str] = {"E": "equity", "C": "etf"}

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
# `<code>　<name>` — the separator is the full-width space U+3000 in the raw
# HTML, but `_clean()` collapses any whitespace (including U+3000) to a
# single ASCII space, so the matcher accepts plain whitespace here.
CODE_NAME_RE = re.compile(r"^(?P<code>[A-Za-z0-9]+)\s+(?P<name>.+)$")


@dataclass(frozen=True)
class IsinRow:
    symbol: str
    name: str
    asset_class: str  # "equity" | "etf"


def _clean(text: str) -> str:
    return WS_RE.sub(" ", TAG_RE.sub("", text)).replace("&nbsp;", " ").strip()


def _fetch(url: str, *, timeout: int = 60) -> str:
    # TWSE's TLS cert lacks a Subject Key Identifier, which Python's default
    # verifier rejects but curl accepts. Shell out to curl so the script
    # works without disabling cert verification.
    proc = subprocess.run(
        ["curl", "-sS", "--fail", "--max-time", str(timeout), "-A", "findb-backfill/1.0", url],
        capture_output=True,
        check=True,
    )
    return proc.stdout.decode("big5", errors="ignore")


def parse_isin_html(html: str) -> list[IsinRow]:
    """Parse one ISIN public page into rows we care about."""
    rows: list[IsinRow] = []
    for tr in ROW_RE.findall(html):
        cells = [_clean(c) for c in CELL_RE.findall(tr)]
        # Header rows and section dividers have fewer than 7 cells or carry a
        # colspan blob; skip anything that doesn't have a CFICode in slot 6.
        if len(cells) < 7:
            continue
        code_name = cells[0]
        cficode = cells[5]
        if not cficode:
            continue
        asset_class = ASSET_CLASS_BY_CFI.get(cficode[:1])
        if asset_class is None:
            continue
        m = CODE_NAME_RE.match(code_name)
        if not m:
            continue
        rows.append(
            IsinRow(
                symbol=m.group("code").strip(),
                name=m.group("name").strip(),
                asset_class=asset_class,
            )
        )
    return rows


def fetch_all_rows() -> list[IsinRow]:
    rows: list[IsinRow] = []
    for label, url in (("上市/TWSE", TWSE_URL), ("上櫃/TPEX", TPEX_URL)):
        logger.info("fetching %s", label)
        html = _fetch(url)
        page_rows = parse_isin_html(html)
        logger.info("  parsed %d rows", len(page_rows))
        rows.extend(page_rows)
    return rows


def build_name_map(rows: Iterable[IsinRow]) -> dict[tuple[str, str], str]:
    """Map (asset_class, symbol) → name. First entry wins on collisions."""
    out: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row.asset_class, row.symbol)
        out.setdefault(key, row.name)
    return out


async def backfill(apply: bool) -> tuple[int, int, int]:
    """Returns (name_updates, currency_updates, unmatched_symbols)."""
    rows = fetch_all_rows()
    name_map = build_name_map(rows)
    logger.info("built %d unique (asset_class, symbol) entries from ISIN", len(name_map))

    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    name_updates = 0
    currency_updates = 0
    unmatched: list[tuple[str, str]] = []

    async with session_maker() as session:
        stmt = select(Instrument).where(Instrument.market == "TW")
        result = await session.execute(stmt)
        instruments = result.scalars().all()
        logger.info("scanning %d TW instruments", len(instruments))

        for inst in instruments:
            key = (inst.asset_class, inst.symbol)
            name = name_map.get(key)
            if name and inst.name is None:
                if apply:
                    inst.name = name
                name_updates += 1
            elif name is None and inst.name is None:
                unmatched.append(key)

            if inst.currency is None:
                if apply:
                    inst.currency = "TWD"
                currency_updates += 1

        if apply:
            await session.commit()
            logger.info("committed updates")
        else:
            logger.info("dry-run: no changes committed")

    await engine.dispose()

    if unmatched:
        logger.warning("unmatched (no name in ISIN): %d", len(unmatched))
        for key in unmatched[:20]:
            logger.warning("  %s/%s", *key)
        if len(unmatched) > 20:
            logger.warning("  ...and %d more", len(unmatched) - 20)

    return name_updates, currency_updates, len(unmatched)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates. Without this flag the script runs as a dry run.",
    )
    args = parser.parse_args()

    name_updates, currency_updates, unmatched = asyncio.run(backfill(apply=args.apply))
    verb = "applied" if args.apply else "would apply"
    logger.info(
        "%s: name=%d currency=%d unmatched=%d",
        verb,
        name_updates,
        currency_updates,
        unmatched,
    )


if __name__ == "__main__":
    main()
