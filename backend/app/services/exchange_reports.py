"""Keep each exchange report a delivery cites, once, in the canonical bucket.

The producer stores the report gzip-compressed in the raw bucket and every
member delivery cites it.  The first delivery to cite a report proves the gzip
and the report inside it, copies the gzip to the canonical bucket and indexes
it; later deliveries of the same report only find the index row.  The object is
written before the index row, in the caller's transaction, so an index row
never names an object that was not stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import zlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import ExchangeReportArchive
from app.schemas.ingress import ExchangeDailyQuoteBatch
from app.services.object_storage import (
    CanonicalPublisher,
    ObjectChangedError,
    ObjectIntegrityError,
    RawObjectReader,
)

KEY_PREFIX = "archive/v1"
CONTENT_TYPE = "application/gzip"


class ReportRejectedError(ValueError):
    """The cited report is not the one the delivery describes; a data error."""

    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


def archive_key(source: str, batch: ExchangeDailyQuoteBatch) -> str:
    """Name the archived gzip by its own content, so a stored object never changes."""
    day = batch.data_date
    return (
        f"{KEY_PREFIX}/{source}/daily_quotation/board={batch.report.board}/"
        f"year={day:%Y}/{day:%Y%m%d}/{batch.source_raw_sha256}.htm.gz"
    )


def prove_gzip(body: bytes, *, gzip_bytes: int, original_bytes: int, original_sha256: str) -> None:
    """The gzip holds exactly the file the delivery describes, and nothing more."""
    if len(body) != gzip_bytes:
        raise ReportRejectedError("gzip_bytes")
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        # One byte more than declared proves the declared size without trusting it.
        original = inflater.decompress(body, original_bytes + 1)
    except zlib.error:
        raise ReportRejectedError("gzip") from None
    if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise ReportRejectedError("gzip")
    if len(original) != original_bytes:
        raise ReportRejectedError("original_bytes")
    if hashlib.sha256(original).hexdigest() != original_sha256:
        raise ReportRejectedError("original_sha256")


def _prove_report(body: bytes, batch: ExchangeDailyQuoteBatch) -> None:
    report = batch.report
    prove_gzip(
        body,
        gzip_bytes=report.gzip_bytes,
        original_bytes=report.original_bytes,
        original_sha256=report.original_sha256,
    )


def put_object(publisher: CanonicalPublisher, key: str, body: bytes) -> str:
    """Create the object; adopt an identical one a failed earlier attempt left behind."""
    try:
        return publisher.write(key, body, content_type=CONTENT_TYPE, expected_etag=None)
    except ObjectChangedError:
        stored = publisher.read(key, max_bytes=len(body))
        if stored.body != body:
            raise ObjectIntegrityError("archived report differs from its content key") from None
        return stored.etag


async def archive_report(
    db: AsyncSession,
    reader: RawObjectReader,
    publisher: CanonicalPublisher,
    *,
    source: str,
    batch: ExchangeDailyQuoteBatch,
    run_id: UUID,
) -> ExchangeReportArchive:
    """Return the archive row of the cited report, archiving it on first use.

    Raises ``ReportRejectedError`` for a report that is not what the delivery
    says, raw ``ObjectNotFoundError``/``ObjectIntegrityError`` for a cited
    object that is missing or altered, and ``ObjectStorageError`` for a storage
    failure the queue retries.
    """
    report = batch.report
    stored = await db.scalar(
        select(ExchangeReportArchive)
        .where(ExchangeReportArchive.original_sha256 == report.original_sha256)
        .order_by(ExchangeReportArchive.created_at)
        .limit(1)
    )
    if stored is not None:
        if (stored.source, stored.board, stored.trade_date) != (
            source,
            report.board,
            batch.data_date,
        ):
            raise ReportRejectedError("report_identity")
        if stored.gzip_sha256 == batch.source_raw_sha256:
            return stored

    body = await asyncio.to_thread(
        reader.read,
        batch.source_raw_ref,
        sha256=batch.source_raw_sha256,
        max_bytes=report.gzip_bytes,
    )
    await asyncio.to_thread(_prove_report, body, batch)
    if stored is not None:
        # Another compression of a report already kept: proven, not kept twice.
        return stored
    key = archive_key(source, batch)
    etag = await asyncio.to_thread(put_object, publisher, key, body)
    row = ExchangeReportArchive(
        source=source,
        board=report.board,
        trade_date=batch.data_date,
        gzip_sha256=batch.source_raw_sha256,
        gzip_bytes=report.gzip_bytes,
        original_sha256=report.original_sha256,
        original_bytes=report.original_bytes,
        source_url=report.url,
        source_last_modified=report.last_modified,
        source_etag=report.etag,
        object_key=key,
        object_etag=etag,
        run_id=run_id,
    )
    db.add(row)
    await db.flush()
    return row
