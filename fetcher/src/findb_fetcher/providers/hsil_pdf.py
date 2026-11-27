"""Read an HSIL constituent report PDF into its cells, column by column.

The report is a table drawn as positioned characters.  Text extraction that
follows the drawing order merges neighbouring cells, and the HSI report marks a
constituent's sub-index only by which weighting column holds a value, so the
cells are rebuilt from each character's position: its column is the one whose
left edge it follows, its record the nearest record date above it.  A cell that
wraps keeps the space it wrapped at, so its lines are joined as they are.

Run as ``python -I -m findb_fetcher.providers.hsil_pdf SERIES`` with the PDF on
stdin, it prints the records as JSON: the parent process bounds the time and
memory a malformed document may take.
"""

from __future__ import annotations

import io
import json
import re
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

# Each report's column names and left edges, in points; the header must match.
LAYOUTS: dict[str, tuple[tuple[str, float, str], ...]] = {
    "hsi": (
        ("date", 20.0, "交易日TradeDate"),
        ("index", 82.4, "指數Index"),
        ("code", 244.9, "股份代號StockCode"),
        ("name", 307.2, "股份名稱StockName"),
        ("listed", 369.6, "上市交易所ExchangeListed"),
        ("industry", 432.0, "行業Industry"),
        ("currency", 494.4, "交易貨幣TradingCurrency"),
        ("close", 556.8, "收市價ClosingPrice"),
        ("change", 619.1, "百分比變動%Change"),
        ("contribution", 681.5, "對指數升跌影響IndexPointContribution"),
        ("weight", 743.9, "比重(%)Weighting(%)"),
        ("weight_fin", 806.3, "於恒生金融分類指數比重(%)WeightinginHSI-Finance(%)"),
        ("weight_uti", 868.7, "於恒生公用事業分類指數比重(%)WeightinginHSI-Utilities(%)"),
        ("weight_prop", 931.0, "於恒生地產分類指數比重(%)WeightinginHSI-Properties(%)"),
        (
            "weight_ci",
            993.4,
            "於恒生工商業分類指數比重(%)WeightinginHSI-Commerce&Industry(%)",
        ),
        ("dividend_yield", 1055.8, "股息率DividendYield(%)"),
        ("pe_ratio", 1118.2, "市價/盈利率P/ERatio(Times)"),
    ),
    "hsci": (
        ("date", 20.0, "交易日TradeDate"),
        ("index", 119.8, "指數Index"),
        ("code", 282.3, "股份代號StockCode"),
        ("name", 382.1, "股份名稱StockName"),
        ("listed", 481.9, "上市交易所ExchangeListed"),
        ("industry", 581.7, "行業Industry"),
        ("currency", 681.5, "交易貨幣TradingCurrency"),
        ("close", 781.3, "收市價ClosingPrice"),
        ("change", 881.1, "百分比變動%Change"),
        ("contribution", 980.9, "對指數升跌影響IndexPointContribution"),
        ("weight", 1080.7, "比重(%)Weighting(%)"),
    ),
}
# A character this far left of a column edge still belongs to it.
EDGE_TOLERANCE = 0.5
# Below this height only the page footer is printed.
FOOTER_TOP = 30.0
# A gap wider than this between two characters of one line is a space.
WORD_GAP = 1.0
_FOOTER = re.compile(r"Page \d+")
_DATE = re.compile(r"\d{8}")


class ReportLayoutError(ValueError):
    """The PDF is not a constituent report of the expected layout."""


@dataclass(frozen=True)
class _Char:
    x0: float
    x1: float
    y: float
    text: str


def normalize(text: str) -> str:
    """Printed text as plain text.

    No-break spaces and hyphens become ordinary ones, and a CJK compatibility
    ideograph (some reports draw 易 as U+F9E0) its canonical character.
    """
    return unicodedata.normalize("NFC", text.replace("\xa0", " ").replace("\u2011", "-"))


def squeeze(text: str) -> str:
    """Text without any whitespace, to compare against a known label."""
    return re.sub(r"\s+", "", normalize(text))


def _line(chars: Sequence[_Char]) -> str:
    text = ""
    previous: _Char | None = None
    for char in sorted(chars, key=lambda item: item.x0):
        if previous is not None and char.x0 - previous.x1 > WORD_GAP:
            text += " "
        text += char.text
        previous = char
    return text


def cell_text(chars: Sequence[_Char]) -> str:
    """A cell's lines top to bottom, joined as printed (a wrap keeps its space)."""
    lines: dict[float, list[_Char]] = defaultdict(list)
    for char in chars:
        lines[char.y].append(char)
    return "".join(_line(lines[y]) for y in sorted(lines, reverse=True))


def _pages(original: bytes) -> Iterator[list[_Char]]:
    from pdfminer.converter import PDFPageAggregator
    from pdfminer.layout import LTChar
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser

    document = PDFDocument(PDFParser(io.BytesIO(original)))
    resources = PDFResourceManager(caching=True)
    # No layout analysis: only each character's own position is used.
    device = PDFPageAggregator(resources, laparams=None)
    interpreter = PDFPageInterpreter(resources, device)
    for page in PDFPage.create_pages(document):
        interpreter.process_page(page)
        yield [
            _Char(item.x0, item.x1, round(item.y0 * 2) / 2, item.get_text())
            for item in device.get_result()
            if isinstance(item, LTChar)
        ]


def _column(edges: Sequence[float], x: float) -> int | None:
    found = None
    for index, edge in enumerate(edges):
        if x >= edge - EDGE_TOLERANCE:
            found = index
    return found


def records(original: bytes, series: str) -> list[dict[str, str]]:
    """Every record of the report as ``{column: text}``, in printed order.

    Raises ``ReportLayoutError`` for a page whose header, footer or records do
    not have the known layout.
    """
    layout = LAYOUTS[series]
    names = [name for name, _, _ in layout]
    edges = [edge for _, edge, _ in layout]
    found: list[dict[str, str]] = []
    pages = 0
    for chars in _pages(original):
        pages += 1
        body = [char for char in chars if char.y > FOOTER_TOP]
        footer = [char for char in chars if char.y <= FOOTER_TOP]
        if _FOOTER.fullmatch(normalize(cell_text(footer)).strip()) is None:
            raise ReportLayoutError("constituent report page footer differs")
        by_column: dict[int, list[_Char]] = defaultdict(list)
        for char in body:
            column = _column(edges, char.x0)
            if column is None:
                raise ReportLayoutError("constituent report prints outside its columns")
            by_column[column].append(char)
        date_lines: dict[float, list[_Char]] = defaultdict(list)
        for char in by_column[0]:
            date_lines[char.y].append(char)
        starts = sorted(
            (y for y, line in date_lines.items() if _DATE.fullmatch(_line(line))), reverse=True
        )
        if not starts:
            raise ReportLayoutError("constituent report page has no records")
        top = starts[0] + 1
        header: dict[int, list[_Char]] = defaultdict(list)
        for column, items in by_column.items():
            header[column] = [char for char in items if char.y > top]
        for index, (_, _, label) in enumerate(layout):
            if squeeze(cell_text(header[index])) != label:
                raise ReportLayoutError("constituent report header differs")
        for number, start in enumerate(starts):
            low = starts[number + 1] + 1 if number + 1 < len(starts) else FOOTER_TOP
            record: dict[str, str] = {}
            for column, items in by_column.items():
                cell = [char for char in items if low < char.y <= start + 1]
                if cell:
                    record[names[column]] = cell_text(cell)
            found.append(record)
    if not pages:
        raise ReportLayoutError("constituent report has no pages")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or arguments[0] not in LAYOUTS:
        return 2
    original = sys.stdin.buffer.read()
    try:
        result: dict[str, Any] = {"records": records(original, arguments[0])}
    except ReportLayoutError as exc:
        result = {"error": "layout", "detail": str(exc)}
    except Exception:  # noqa: BLE001 - any parser failure is an unreadable document
        result = {"error": "unreadable"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
