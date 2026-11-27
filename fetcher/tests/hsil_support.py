"""Hand-made HSIL documents: a constituent report PDF and a change workbook.

The PDF draws each cell's lines at the report's column edges with a standard
font whose encoding names every non-ASCII character by its ``uniXXXX`` glyph,
so the reader sees the same characters and positions a real report gives it.
No HSIL file is committed.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping, Sequence
from xml.sax.saxutils import escape

from findb_fetcher.providers.hsil_pdf import LAYOUTS

# Every character is 4.5 points wide at 9 points: 13 fit in the narrowest column.
_WIDTH = 500
_SIZE = 9
LINE = 12
HEADER_TOP = 794.0
RECORD_TOP = 640.0
RECORD_STEP = 60.0
LINE_STEP = 13.0

Record = Mapping[str, Sequence[str]]


def chunk(text: str, size: int = LINE) -> list[str]:
    """Wrap ``text`` every ``size`` characters, as a narrow column wraps it."""
    return [text[start : start + size] for start in range(0, len(text), size)] or [""]


def _pdf_string(text: str, codes: dict[str, int]) -> bytes:
    out = bytearray()
    for char in text:
        if char.isascii():
            if char in "()\\":
                out += b"\\"
            out += char.encode()
        else:
            out += bytes([codes[char]])
    return b"(" + bytes(out) + b")"


def report_pdf(
    series: str,
    pages: Sequence[Sequence[Record]],
    *,
    footer: str = "Page",
    header: Mapping[str, str] | None = None,
) -> bytes:
    """A constituent report of ``pages`` of records, each ``{column: lines}``.

    ``header`` replaces a column's printed label.
    """
    layout = LAYOUTS[series]
    edges = {name: edge for name, edge, _ in layout}
    drawn: list[list[tuple[float, float, str]]] = []
    for number, records in enumerate(pages, start=1):
        items: list[tuple[float, float, str]] = []
        for name, edge, label in layout:
            for line_number, line in enumerate(chunk((header or {}).get(name, label))):
                items.append((edge, HEADER_TOP - LINE_STEP * line_number, line))
        for index, record in enumerate(records):
            top = RECORD_TOP - RECORD_STEP * index
            for column, lines in record.items():
                for line_number, line in enumerate(lines):
                    items.append((edges[column], top - LINE_STEP * line_number, line))
        items.append((581.3, 9.0, f"{footer} {number}"))
        drawn.append(items)
    wide = sorted(
        {char for items in drawn for *_, text in items for char in text if not char.isascii()}
    )
    if len(wide) > 127:
        raise ValueError("a test report may use at most 127 non-ASCII characters")
    codes = {char: 128 + offset for offset, char in enumerate(wide)}
    differences = " ".join(f"/uni{ord(char):04X}" for char in wide)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        None,  # pages, once the page objects are numbered
        (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /FirstChar 0 /LastChar 255 "
            b"/Widths [" + b" ".join([str(_WIDTH).encode()] * 256) + b"] "
            b"/Encoding << /Type /Encoding /BaseEncoding /WinAnsiEncoding "
            b"/Differences [128 " + differences.encode() + b"] >> >>"
        ),
    ]
    kids = []
    for items in drawn:
        stream = b"".join(
            b"BT /F1 %d Tf %.1f %.1f Td %s Tj ET\n" % (_SIZE, x, y, _pdf_string(text, codes))
            for x, y, text in items
        )
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        content = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1191 842] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content
        )
        kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        len(kids),
    )
    body = io.BytesIO()
    body.write(b"%PDF-1.4\n")
    offsets = []
    for number, value in enumerate(objects, start=1):
        assert value is not None
        offsets.append(body.tell())
        body.write(b"%d 0 obj\n%s\nendobj\n" % (number, value))
    start = body.tell()
    body.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        body.write(b"%010d 00000 n \n" % offset)
    body.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)
    )
    return body.getvalue()


def constituent(
    *,
    index: str,
    code: str,
    name: str,
    industry: str,
    close: str,
    weight: str,
    day: str = "20261127",
    **extra: str,
) -> dict[str, list[str]]:
    """One record, each cell wrapped as the report wraps it."""
    record = {
        "date": [day],
        # The index column is the widest: 36 characters.
        "index": chunk(index, 30),
        "code": [code],
        "name": chunk(name),
        "listed": ["Hong Kong ", "香港"],
        "industry": chunk(industry),
        "currency": ["HKD"],
        "close": [close],
        "change": ["‑1.37"],
        "contribution": ["0.09"],
        "weight": [weight],
    }
    record.update({key: [value] for key, value in extra.items()})
    return record


# --- workbooks --------------------------------------------------------------

HEADER = (
    "Effective Date 生效日期",
    "No. of Constituents",
    "Change 變動",
    "Count 數目",
    "Stock Code 股份代號",
    "Listing Place 上市地點",
    "Stock Name",
    "股份名稱",
)
NOTE = "1. The number of constituents refers to the actual number of stocks."


def _cell(reference: str, value: object) -> str:
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    return f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def sheet_rows(
    name_en: str, name_zh: str, changes: Sequence[Sequence[object]]
) -> list[list[object | None]]:
    """A sheet as HSIL lays it out: title, header, changes newest first, notes."""
    return [
        [f"Constituent Changes in {name_en}"],
        [f"{name_zh}之成份股變動"],
        [],
        [],
        list(HEADER),
        [None, "(After Change) 成份股數目 (變動後)"],
        *[list(change) for change in changes],
        [],
        [NOTE],
    ]


def workbook(sheets: Mapping[str, Sequence[Sequence[object | None]]]) -> bytes:
    """An XLSX of ``sheets`` (name -> rows); empty rows are left out, as Excel does."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        entries, links = [], []
        for number, (name, rows) in enumerate(sheets.items(), start=1):
            entries.append(f'<sheet name="{escape(name)}" sheetId="{number}" r:id="rId{number}"/>')
            links.append(
                f'<Relationship Id="rId{number}" Type="http://schemas.openxmlformats.org/'
                f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{number}.xml"/>'
            )
            xml_rows = []
            for row_number, row in enumerate(rows, start=1):
                cells = "".join(
                    _cell(f"{column}{row_number}", value)
                    for column, value in zip("ABCDEFGH", row, strict=False)
                    if value is not None
                )
                if cells:
                    xml_rows.append(f'<row r="{row_number}">{cells}</row>')
            archive.writestr(
                f"xl/worksheets/sheet{number}.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>",
            )
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{''.join(entries)}</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{''.join(links)}</Relationships>",
        )
    return buffer.getvalue()
