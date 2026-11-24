"""Builders for index reference requests: small, synthetic publisher documents."""

from __future__ import annotations

import gzip
import hashlib
from datetime import date, datetime, timezone
from typing import Any

from tests.intraday_support import ACCOUNT, RAW_BUCKET

SOURCE = "hkex"
OBSERVED = date(2026, 11, 24)
FETCHED = datetime(2026, 11, 24, 14, 30, tzinfo=timezone.utc)
HSI = {"code": "00001.00", "name_en": "Hang Seng Index", "name_zh": "恒生指數"}
HSI_FINANCE = {
    "code": "00001.01",
    "name_en": "Hang Seng Index - Finance",
    "name_zh": "恒生金融分類指數",
    "parent_code": "00001.00",
}
HSCI = {"code": "00011.00", "name_en": "Hang Seng Composite Index", "name_zh": "恒生綜合指數"}
HSCI_UTILITIES = {
    "code": "00011.07",
    "name_en": "Hang Seng Composite Industry Index - Utilities",
    "parent_code": "00011.00",
    "classification": {
        "scheme": "hsics",
        "code": "40",
        "name_en": "Utilities",
        "name_zh": "公用事業",
    },
}


def document(
    kind: str,
    *,
    series: str,
    document_date: date,
    part_key: str = "document",
    original: bytes = b"%PDF-1.4 synthetic publisher document\n",
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    """The gzip the producer stores, its batch evidence fields, and the document fields."""
    body = gzip.compress(original, mtime=0)
    digest = hashlib.sha256(body).hexdigest()
    batch = {
        "data_date": OBSERVED.isoformat(),
        "delivery_mode": "full_snapshot",
        "source_raw_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/hkex/hsil/0123456789abcdef/{digest}.pdf.gz",
        "source_raw_sha256": digest,
    }
    evidence = {
        "publisher": "hsil",
        "kind": kind,
        "series": series,
        "url": (
            "https://www.hsi.com.hk/api/wsit-hsi-ddoc-ea-public-website-proxy/v1/content/gcs/"
            "download?resource=/public-website/webapp/contents/en/indexes/report/hsi/con_23Nov26.pdf"
        ),
        "document_date": document_date.isoformat(),
        "part_key": part_key,
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "original_bytes": len(original),
        "gzip_bytes": len(body),
        "last_modified": None,
        "etag": None,
    }
    return body, batch, evidence


def request(schema_id: str, dataset_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset_key": dataset_key,
        "schema_id": schema_id,
        "schema_version": 1,
        "source": SOURCE,
        "request_key": f"{schema_id}-request",
        "idempotency_key": f"{schema_id}-delivery",
        "fetched_at": FETCHED.isoformat(),
        "payload": payload,
    }


def constituent(index_code: str, symbol: str, weight: str, **values: Any) -> dict[str, Any]:
    return {
        "index_code": index_code,
        "trade_date": "2026-11-23",
        "symbol": symbol,
        "name_en": f"STOCK {symbol}",
        "name_zh": None,
        "listing_place": "Hong Kong",
        "currency": "HKD",
        "close": "10.5",
        "weight_pct": weight,
        "industry": None,
        **values,
    }


def constituents_payload(
    rows: list[dict[str, Any]] | None = None,
    indexes: list[dict[str, Any]] | None = None,
    *,
    trade_date: date = date(2026, 11, 23),
) -> dict[str, Any]:
    rows = (
        rows
        if rows is not None
        else [
            constituent("00001.00", "00005", "60"),
            constituent("00001.00", "00011", "40"),
            constituent("00001.01", "00005", "100"),
        ]
    )
    indexes = (
        indexes
        if indexes is not None
        else [
            {**HSI, "constituent_count": 2},
            {**HSI_FINANCE, "constituent_count": 1},
        ]
    )
    _, batch, evidence = document("constituent_report", series="hsi", document_date=trade_date)
    return {
        "batch": {
            **batch,
            "declared_record_count": len(rows),
            "document": evidence,
            "indexes": indexes,
        },
        "data": rows,
    }


def change(ordinal: int, effective_date: str, action: str, symbol: str, after: int, **values):
    return {
        "ordinal": ordinal,
        "effective_date": effective_date,
        "action": action,
        "symbol": symbol,
        "name_en": f"STOCK {symbol}",
        "name_zh": None,
        "listing_place": "Hong Kong",
        "constituents_after": after,
        "count_change": None,
        **values,
    }


def changes_payload(
    rows: list[dict[str, Any]] | None = None,
    *,
    index: dict[str, Any] | None = None,
    history_start: str = "2008-10-08",
    document_date: date = date(2026, 10, 23),
) -> dict[str, Any]:
    rows = (
        rows
        if rows is not None
        else [
            change(1, "2026-10-23", "delete", "02555", 580, count_change=-1),
            change(2, "2008-10-08", "add", "00005", 581),
        ]
    )
    index = index or HSCI
    _, batch, evidence = document(
        "change_workbook", series="hsci", document_date=document_date, part_key=index["code"]
    )
    return {
        "batch": {
            **batch,
            "declared_record_count": len(rows),
            "document": evidence,
            "index": index,
            "history_start": history_start,
        },
        "data": rows,
    }
