"""Keep each publisher document a reference delivery cites, once, in the canonical bucket.

The producer stores the document gzip-compressed in the raw bucket.  The first
delivery that cites a document proves the gzip and the document inside it,
copies the gzip to the canonical bucket and indexes it; a later delivery of the
same document, even compressed differently, finds the index row.  The object is
written before the index row, in the caller's transaction, so an index row never
names an object that was not stored.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.index_reference import ReferenceDocumentArchive
from app.schemas.ingress import IndexDocumentBatch
from app.services.exchange_reports import (
    CONTENT_TYPE,
    KEY_PREFIX,
    ReportRejectedError,
    prove_gzip,
    put_object,
)
from app.services.object_storage import CanonicalPublisher, RawObjectReader

EXTENSIONS = {"constituent_report": "pdf", "change_workbook": "xlsx"}

__all__ = ["CONTENT_TYPE", "ReportRejectedError", "archive_document", "archive_key"]


def archive_key(source: str, batch: IndexDocumentBatch) -> str:
    """Name the archived gzip by its own content, so a stored object never changes."""
    document = batch.document
    day = document.document_date
    return (
        f"{KEY_PREFIX}/{source}/{document.publisher}/{document.kind}/series={document.series}/"
        f"year={day:%Y}/{day:%Y%m%d}/{batch.source_raw_sha256}.{EXTENSIONS[document.kind]}.gz"
    )


async def archive_document(
    db: AsyncSession,
    reader: RawObjectReader,
    publisher: CanonicalPublisher,
    *,
    source: str,
    batch: IndexDocumentBatch,
    run_id: UUID,
) -> ReferenceDocumentArchive:
    """Return the archive row of the cited document, archiving it on first use.

    Raises ``ReportRejectedError`` for a document that is not what the delivery
    says, raw ``ObjectNotFoundError``/``ObjectIntegrityError`` for a cited object
    that is missing or altered, and ``ObjectStorageError`` for a storage failure
    the queue retries.
    """
    document = batch.document
    stored = await db.scalar(
        select(ReferenceDocumentArchive)
        .where(ReferenceDocumentArchive.original_sha256 == document.original_sha256)
        .order_by(ReferenceDocumentArchive.created_at)
        .limit(1)
    )
    identity = (source, document.publisher, document.kind, document.series, document.document_date)
    if stored is not None:
        if (
            stored.source,
            stored.publisher,
            stored.kind,
            stored.series,
            stored.document_date,
        ) != identity:
            raise ReportRejectedError("document_identity")
        if stored.gzip_sha256 == batch.source_raw_sha256:
            return stored

    body = await asyncio.to_thread(
        reader.read,
        batch.source_raw_ref,
        sha256=batch.source_raw_sha256,
        max_bytes=document.gzip_bytes,
    )
    await asyncio.to_thread(
        prove_gzip,
        body,
        gzip_bytes=document.gzip_bytes,
        original_bytes=document.original_bytes,
        original_sha256=document.original_sha256,
    )
    if stored is not None:
        # Another compression of a document already kept: proven, not kept twice.
        return stored
    key = archive_key(source, batch)
    etag = await asyncio.to_thread(put_object, publisher, key, body)
    row = ReferenceDocumentArchive(
        source=source,
        publisher=document.publisher,
        kind=document.kind,
        series=document.series,
        document_date=document.document_date,
        gzip_sha256=batch.source_raw_sha256,
        gzip_bytes=document.gzip_bytes,
        original_sha256=document.original_sha256,
        original_bytes=document.original_bytes,
        source_url=document.url,
        source_last_modified=document.last_modified,
        source_etag=document.etag,
        object_key=key,
        object_etag=etag,
        run_id=run_id,
    )
    db.add(row)
    await db.flush()
    return row
