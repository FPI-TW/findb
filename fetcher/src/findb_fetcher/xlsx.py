"""A bounded reader of the cell text of an XLSX workbook, sheet by sheet.

Only what a published list needs: each sheet's rows as ``{column letter: text}``
with shared strings resolved.  A workbook that expands beyond the bounds, or
whose parts do not fit together, is rejected rather than read in part.
"""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOCUMENT_RELATIONSHIP = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS = {"s": _MAIN, "r": _RELATIONSHIPS}
MAX_PARTS = 100
MAX_EXPANDED_BYTES = 32 * 1024 * 1024

Rows = list[dict[str, str]]


class XlsxError(ValueError):
    """The bytes are not a workbook within the reader's bounds."""


def _sheet_rows(root: ET.Element, strings: list[str]) -> Rows:
    result = []
    for row in root.findall(".//s:row", _NS):
        cells = {}
        for cell in row.findall("s:c", _NS):
            value = cell.find("s:v", _NS)
            text = value.text or "" if value is not None else "".join(cell.itertext())
            if cell.get("t") == "s":
                text = strings[int(text)]
            column = re.sub(r"[0-9]", "", cell.attrib["r"])
            cells[column] = text.strip()
        result.append(cells)
    return result


def _open(raw: bytes) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(io.BytesIO(raw))
    parts = archive.infolist()
    if len(parts) > MAX_PARTS or sum(item.file_size for item in parts) > MAX_EXPANDED_BYTES:
        archive.close()
        raise XlsxError("workbook expansion exceeds size limit")
    return archive


def _strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    return [
        "".join(node.itertext())
        for node in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("s:si", _NS)
    ]


def first_sheet_rows(raw: bytes) -> Rows:
    """The rows of ``xl/worksheets/sheet1.xml``."""
    try:
        with _open(raw) as archive:
            return _sheet_rows(
                ET.fromstring(archive.read("xl/worksheets/sheet1.xml")), _strings(archive)
            )
    except XlsxError:
        raise
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError, IndexError) as exc:
        raise XlsxError("workbook shape is invalid") from exc


def sheets(raw: bytes) -> dict[str, Rows]:
    """Every sheet's rows by sheet name, in workbook order."""
    try:
        with _open(raw) as archive:
            strings = _strings(archive)
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            links = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {
                link.attrib["Id"]: link.attrib["Target"]
                for link in links.findall("r:Relationship", _NS)
            }
            result: dict[str, Rows] = {}
            for sheet in workbook.findall("s:sheets/s:sheet", _NS):
                name = sheet.attrib["name"]
                target = targets[sheet.attrib[f"{{{_DOCUMENT_RELATIONSHIP}}}id"]]
                part = (
                    target.lstrip("/")
                    if target.startswith("/")
                    else posixpath.normpath(posixpath.join("xl", target))
                )
                if name in result:
                    raise XlsxError("workbook names a sheet twice")
                result[name] = _sheet_rows(ET.fromstring(archive.read(part)), strings)
            return result
    except XlsxError:
        raise
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError, IndexError) as exc:
        raise XlsxError("workbook shape is invalid") from exc
