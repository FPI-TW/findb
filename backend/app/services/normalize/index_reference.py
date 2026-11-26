"""Normalizers for index reference documents: constituent reports and change workbooks.

Each delivery archives its document once, stores what the document states and
rebuilds its family's membership history from everything stored, all in one
transaction.  Which deliveries a day needs is the daily plan's business: a
report is one member per family, a change workbook one member per index.  A document that contradicts
itself (weights that do not sum to 100, a member outside its parent) is
rejected whole and stores nothing; disagreements between documents are
reported as warnings by the rebuild and never block.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.index_reference import (
    IndexChangeEvent,
    IndexDefinition,
    IndexSnapshot,
    IndexSnapshotConstituent,
    ReferenceDocumentArchive,
)
from app.models.registry import IngestionRun
from app.schemas.ingress import (
    IndexChangesPayload,
    IndexConstituentsPayload,
    IndexDescriptor,
)
from app.services.dq.validators import DQIssueRecord
from app.services.index_membership import lock_family, rebuild_family, summarize
from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.normalize.intraday import canonical_publisher, raw_reader
from app.services.object_storage import ObjectIntegrityError, ObjectNotFoundError
from app.services.reference_documents import ReportRejectedError, archive_document

# A published weight list may round to a few hundredths either side of 100.
WEIGHT_TOLERANCE = Decimal("0.1")
# Rows per multi-row upsert; a full workbook sheet is a few thousand rows.
UPSERT_CHUNK = 1000


class DocumentRejectedError(ValueError):
    def __init__(self, rule: str, **detail: object) -> None:
        super().__init__(rule)
        self.rule = rule
        self.detail = detail


def _day(value: date) -> datetime:
    return datetime.combine(value, time.min, timezone.utc)


class _IndexDocumentNormalizer(BaseNormalizer):
    payload_model: type[IndexConstituentsPayload] | type[IndexChangesPayload]

    def map_fields(self, raw_data: dict) -> list[Any]:
        values = raw_data.get("data", [])
        return values if isinstance(values, list) else []

    async def _reject_all(
        self, result: NormalizeResult, run_id: UUID, rule: str, detail: dict | None = None
    ) -> None:
        issue = DQIssueRecord(
            issue_type="INDEX_DOCUMENT_REJECTED",
            severity="error",
            description=f"Index reference document rejected: {rule}",
            raw_data={"rule": rule, **(detail or {})},
        )
        await self.record_dq_issue(issue, run_id)
        result.dq_issues.append(issue)
        result.failed_records = result.total_records
        result.error_message = issue.description

    async def _warn(
        self, result: NormalizeResult, run_id: UUID, issue_type: str, description: str, **raw: Any
    ) -> None:
        issue = DQIssueRecord(issue_type, "warning", description, raw_data=raw)
        await self.record_dq_issue(issue, run_id)
        result.dq_issues.append(issue)

    async def _definitions(
        self, publisher: str, series: str, indexes: Sequence[IndexDescriptor], **values: Any
    ) -> dict[str, IndexDefinition]:
        """Create or update each named index, parents before children."""
        codes = {index.code for index in indexes} | {
            index.parent_code for index in indexes if index.parent_code
        }
        stored = {
            item.code: item
            for item in (
                await self.db.scalars(
                    select(IndexDefinition).where(
                        IndexDefinition.publisher == publisher, IndexDefinition.code.in_(codes)
                    )
                )
            ).all()
        }
        for item in stored.values():
            if item.series != series:
                raise DocumentRejectedError(
                    "index_series_differs", code=item.code, stored=item.series
                )
        ordered = sorted(indexes, key=lambda index: index.parent_code is not None)
        for index in ordered:
            parent = None
            if index.parent_code is not None:
                parent = stored.get(index.parent_code)
                if parent is None:
                    # Named before its own document arrives; that document completes it.
                    parent = IndexDefinition(
                        publisher=publisher,
                        code=index.parent_code,
                        series=series,
                        name_en=index.parent_code,
                    )
                    self.db.add(parent)
                    await self.db.flush()
                    stored[parent.code] = parent
            found = stored.get(index.code)
            if found is None:
                found = IndexDefinition(publisher=publisher, code=index.code, series=series)
                self.db.add(found)
                stored[index.code] = found
            item = found
            item.name_en = index.name_en
            item.name_zh = index.name_zh or item.name_zh
            # A document that does not state a parent or industry leaves the stored one.
            if parent is not None:
                item.parent_index_id = parent.index_id
            classification = index.classification
            if classification is not None:
                item.classification_scheme = classification.scheme
                item.classification_code = classification.code
                item.classification_name_en = classification.name_en
                item.classification_name_zh = classification.name_zh or item.classification_name_zh
            for key, value in values.get(index.code, {}).items():
                setattr(item, key, value)
            await self.db.flush()
        return stored

    async def _rebuild(
        self, result: NormalizeResult, run: IngestionRun, publisher: str, series: str
    ) -> None:
        rebuilt = await rebuild_family(
            self.db, publisher=publisher, series=series, run_id=run.run_id
        )
        for summary in summarize(rebuilt.findings):
            await self._warn(
                result,
                run.run_id,
                "INDEX_MEMBERSHIP_FINDING",
                f"{summary['count']} {summary['rule']} in {summary['index_code']}",
                **summary,
            )
        if rebuilt.unresolved_open:
            await self._warn(
                result,
                run.run_id,
                "INDEX_MEMBER_UNRESOLVED",
                f"{len(rebuilt.unresolved_open)} current members name no instrument",
                family=series,
                symbols=rebuilt.unresolved_open,
            )

    async def _store_document(
        self,
        payload: Any,
        run: IngestionRun,
        result: NormalizeResult,
        archive: ReferenceDocumentArchive,
    ) -> None:
        raise NotImplementedError

    def _check(self, payload: Any) -> None:
        """Rules a single document must keep; raises ``DocumentRejectedError``."""

    async def _store(self, raw_payload: dict, run: IngestionRun, result: NormalizeResult) -> None:
        try:
            payload = self.payload_model.model_validate(raw_payload)
        except (ValidationError, ValueError):
            await self._reject_all(result, run.run_id, "payload")
            return
        try:
            self._check(payload)
        except DocumentRejectedError as exc:
            await self._reject_all(result, run.run_id, exc.rule, exc.detail)
            return
        source = str(self.dataset_config.get("_ingest_source") or run.source)
        document = payload.batch.document
        savepoint = await self.db.begin_nested()
        try:
            # Serialises archiving too: the same document is archived once.
            await lock_family(self.db, document.publisher, document.series)
            archive = await archive_document(
                self.db,
                raw_reader(),
                canonical_publisher(),
                source=source,
                batch=payload.batch,
                run_id=run.run_id,
            )
            await self._store_document(payload, run, result, archive)
            await self._rebuild(result, run, document.publisher, document.series)
        except (ReportRejectedError, DocumentRejectedError) as exc:
            await savepoint.rollback()
            await self._reject_all(result, run.run_id, exc.rule, getattr(exc, "detail", None))
            return
        except (ObjectNotFoundError, ObjectIntegrityError) as exc:
            await savepoint.rollback()
            await self._reject_all(result, run.run_id, "raw_object", {"reason": str(exc)})
            return
        except BaseException:
            await savepoint.rollback()
            raise
        await savepoint.commit()
        result.success_records = result.total_records

    async def process(
        self, raw_payload: dict, run_id: UUID, *, commit: bool = True
    ) -> NormalizeResult:
        result = NormalizeResult(run_id=run_id, total_records=len(self.map_fields(raw_payload)))
        try:
            await self.update_run_status(run_id, "processing", 0, 0, 0)
            run = await self.db.get(IngestionRun, run_id)
            if run is None:
                raise RuntimeError("ingestion run disappeared during normalization")
            await self._store(raw_payload, run, result)
            await self.update_run_status(
                run_id,
                "completed" if result.failed_records == 0 else "completed_with_errors",
                result.total_records,
                result.success_records,
                result.failed_records,
                result.error_message,
            )
            if commit:
                await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return result


class IndexConstituentsNormalizer(_IndexDocumentNormalizer):
    """Store one constituent report's lists as the snapshots of its trading day."""

    payload_model = IndexConstituentsPayload

    def _check(self, payload: IndexConstituentsPayload) -> None:
        members: dict[str, set[str]] = defaultdict(set)
        weights: dict[str, Decimal] = defaultdict(Decimal)
        for row in payload.data:
            members[row.index_code].add(row.symbol)
            weights[row.index_code] += row.weight_pct
        for code, total in sorted(weights.items()):
            if abs(total - 100) > WEIGHT_TOLERANCE:
                raise DocumentRejectedError(
                    "weights_do_not_sum_to_100", index_code=code, total=str(total)
                )
        indexes = {index.code: index for index in payload.batch.indexes}
        children: dict[str, list[str]] = defaultdict(list)
        for index in indexes.values():
            if index.parent_code is not None:
                children[index.parent_code].append(index.code)
        for parent, codes in sorted(children.items()):
            placed: dict[str, list[str]] = defaultdict(list)
            for code in codes:
                for symbol in members[code]:
                    placed[symbol].append(code)
            outside = sorted(set(placed) - members[parent])
            unplaced = sorted(members[parent] - set(placed))
            repeated = sorted(symbol for symbol, found in placed.items() if len(found) > 1)
            if outside or unplaced or repeated:
                raise DocumentRejectedError(
                    "children_do_not_partition_parent",
                    index_code=parent,
                    outside_parent=outside,
                    in_no_child=unplaced,
                    in_several_children=repeated,
                )
            industries = {
                code: indexes[code].classification.code  # type: ignore[union-attr]
                for code in codes
                if indexes[code].classification is not None
            }
            if not industries:
                continue
            for row in payload.data:
                if row.index_code != parent or row.industry is None:
                    continue
                (child,) = placed[row.symbol]
                if industries.get(child) != row.industry.code:
                    raise DocumentRejectedError(
                        "industry_differs_from_industry_index",
                        symbol=row.symbol,
                        industry=row.industry.code,
                        industry_index=child,
                    )

    async def _store_document(
        self,
        payload: IndexConstituentsPayload,
        run: IngestionRun,
        result: NormalizeResult,
        archive: ReferenceDocumentArchive,
    ) -> None:
        document = payload.batch.document
        stored = await self._definitions(document.publisher, document.series, payload.batch.indexes)
        rows: dict[str, list[Any]] = defaultdict(list)
        for row in payload.data:
            rows[row.index_code].append(row)
        for index in payload.batch.indexes:
            definition = stored[index.code]
            snapshot = await self.db.scalar(
                select(IndexSnapshot).where(
                    IndexSnapshot.index_id == definition.index_id,
                    IndexSnapshot.trade_date == document.document_date,
                )
            )
            if snapshot is not None:
                if snapshot.archive_id == archive.archive_id:
                    continue
                kept = await self.db.get(ReferenceDocumentArchive, snapshot.archive_id)
                if kept is not None and kept.created_at > archive.created_at:
                    # An older version delivered again never replaces a newer one.
                    await self._warn(
                        result,
                        run.run_id,
                        "INDEX_DOCUMENT_STALE",
                        f"{index.code} snapshot of {document.document_date} is older than the stored one",
                        index_code=index.code,
                        trade_date=document.document_date.isoformat(),
                    )
                    continue
                # A republished report replaces the day it speaks for.
                await self._warn(
                    result,
                    run.run_id,
                    "INDEX_SNAPSHOT_REPLACED",
                    f"{index.code} snapshot of {document.document_date} republished",
                    index_code=index.code,
                    trade_date=document.document_date.isoformat(),
                )
                await self.db.delete(snapshot)
                await self.db.flush()
            snapshot = IndexSnapshot(
                index_id=definition.index_id,
                trade_date=document.document_date,
                constituent_count=index.constituent_count,
                archive_id=archive.archive_id,
                run_id=run.run_id,
            )
            self.db.add(snapshot)
            await self.db.flush()
            self.db.add_all(
                IndexSnapshotConstituent(
                    snapshot_id=snapshot.snapshot_id,
                    symbol=row.symbol,
                    name_en=row.name_en,
                    name_zh=row.name_zh,
                    listing_place=row.listing_place,
                    currency=row.currency,
                    close=row.close,
                    weight_pct=row.weight_pct,
                    industry_code=row.industry.code if row.industry else None,
                )
                for row in rows[index.code]
            )
        await self.db.flush()


class IndexChangesNormalizer(_IndexDocumentNormalizer):
    """Store one index's complete change history as its effective changes."""

    payload_model = IndexChangesPayload

    async def _store_document(
        self,
        payload: IndexChangesPayload,
        run: IngestionRun,
        result: NormalizeResult,
        archive: ReferenceDocumentArchive,
    ) -> None:
        batch = payload.batch
        document = batch.document
        stored = await self._definitions(
            document.publisher,
            document.series,
            [batch.index],
            **{batch.index.code: {"history_start": batch.history_start}},
        )
        index_id = stored[batch.index.code].index_id
        newest = await self.db.scalar(
            select(ReferenceDocumentArchive)
            .join(
                IndexChangeEvent, IndexChangeEvent.archive_id == ReferenceDocumentArchive.archive_id
            )
            .where(IndexChangeEvent.index_id == index_id)
            .order_by(
                ReferenceDocumentArchive.document_date.desc(),
                ReferenceDocumentArchive.created_at.desc(),
            )
            .limit(1)
        )
        if (
            newest is not None
            and newest.archive_id != archive.archive_id
            and (
                newest.document_date,
                newest.created_at,
            )
            > (archive.document_date, archive.created_at)
        ):
            # An older workbook delivered again never retracts what a newer one lists.
            await self._warn(
                result,
                run.run_id,
                "INDEX_DOCUMENT_STALE",
                f"{batch.index.code} workbook of {archive.document_date} is older than the stored one",
                index_code=batch.index.code,
                document_date=archive.document_date.isoformat(),
            )
            return
        listed = {(row.effective_date, row.symbol, row.action) for row in payload.data}
        values = [
            {
                "index_id": index_id,
                "effective_date": row.effective_date,
                "action": row.action,
                "symbol": row.symbol,
                "first_seen_run_id": run.run_id,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "listing_place": row.listing_place,
                "constituents_after": row.constituents_after,
                "count_change": row.count_change,
                "source_ordinal": row.ordinal,
                "status": "effective",
                "archive_id": archive.archive_id,
                "last_seen_run_id": run.run_id,
            }
            for row in payload.data
        ]
        refreshed = (
            "name_en",
            "name_zh",
            "listing_place",
            "constituents_after",
            "count_change",
            "source_ordinal",
            "status",
            "archive_id",
            "last_seen_run_id",
        )
        for start in range(0, len(values), UPSERT_CHUNK):
            statement = insert(IndexChangeEvent).values(values[start : start + UPSERT_CHUNK])
            await self.db.execute(
                statement.on_conflict_do_update(
                    constraint="uq_index_change_event",
                    set_={key: getattr(statement.excluded, key) for key in refreshed},
                )
            )
        retracted = [
            event
            for event in (
                await self.db.scalars(
                    select(IndexChangeEvent).where(
                        IndexChangeEvent.index_id == index_id,
                        IndexChangeEvent.status == "effective",
                    )
                )
            ).all()
            if (event.effective_date, event.symbol, event.action) not in listed
        ]
        if retracted:
            await self.db.execute(
                update(IndexChangeEvent)
                .where(IndexChangeEvent.event_id.in_([event.event_id for event in retracted]))
                .values(status="retracted", last_seen_run_id=run.run_id)
            )
            await self._warn(
                result,
                run.run_id,
                "INDEX_CHANGE_RETRACTED",
                f"{len(retracted)} changes of {batch.index.code} are no longer listed",
                index_code=batch.index.code,
                changes=[
                    [event.effective_date.isoformat(), event.action, event.symbol]
                    for event in retracted[:20]
                ],
            )
        await self.db.flush()
