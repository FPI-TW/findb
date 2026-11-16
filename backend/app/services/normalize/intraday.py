"""Normalizer for ``intraday_partition.v1``: store one symbol's delivered trading day."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.models.registry import IngestionRun
from app.schemas.ingress import IntradayPartitionPayload
from app.services.dq.validators import DQIssueRecord
from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.object_storage import (
    CanonicalPublisher,
    ObjectIntegrityError,
    ObjectNotFoundError,
    RawObjectReader,
)
from app.utils.datetime_utils import ensure_utc

if TYPE_CHECKING:
    from app.schemas.ingress import IntradayPartitionRow


@lru_cache(maxsize=1)
def raw_reader() -> RawObjectReader:
    return RawObjectReader()


@lru_cache(maxsize=1)
def canonical_publisher() -> CanonicalPublisher:
    return CanonicalPublisher()


class IntradayPartitionContractNormalizer(BaseNormalizer):
    """Prove the delivered rows object, store it as the canonical day and index it.

    The rows arrive already mapped to the published layout, so nothing here is
    provider-specific.  A day is all or nothing: any rule its rows break leaves
    the stored day and its index untouched and ends the run with a DQ error.
    A storage failure is not a data error and is raised for the queue to retry.
    """

    def map_fields(self, raw_data: dict) -> list[Any]:
        values = raw_data.get("data", [])
        return values if isinstance(values, list) else []

    async def _reject(
        self,
        result: NormalizeResult,
        run_id: UUID,
        rule: str,
        partition: IntradayPartitionRow | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        issue = DQIssueRecord(
            issue_type="INTRADAY_PARTITION_REJECTED",
            severity="error",
            description=f"Intraday partition rejected: {rule}",
            trade_date=(
                datetime.combine(partition.trade_date, time.min, timezone.utc)
                if partition is not None
                else None
            ),
            raw_data={
                "rule": rule,
                **(
                    {"symbol": partition.symbol, "record_kind": partition.record_kind}
                    if partition is not None
                    else {}
                ),
                **(detail or {}),
            },
        )
        await self.record_dq_issue(issue, run_id)
        result.dq_issues.append(issue)
        result.failed_records = 1
        result.error_message = issue.description

    async def _store(self, raw_payload: dict, run: IngestionRun, result: NormalizeResult) -> None:
        try:
            payload = IntradayPartitionPayload.model_validate(raw_payload)
        except ValueError:
            await self._reject(result, run.run_id, "payload")
            return
        partition = payload.data[0]
        defaults = self.dataset_config["defaults"]
        if partition.record_kind != defaults.get("record_kind"):
            await self._reject(result, run.run_id, "record_kind", partition)
            return

        # Parquet support is loaded only where a partition is actually processed.
        from app.services import intraday_partitions as store

        try:
            body = await asyncio.to_thread(
                raw_reader().read,
                partition.rows_ref,
                sha256=partition.rows_sha256,
                max_bytes=partition.rows_bytes,
            )
        except (ObjectNotFoundError, ObjectIntegrityError) as exc:
            await self._reject(result, run.run_id, "rows_object", partition, {"reason": str(exc)})
            return
        try:
            table = await asyncio.to_thread(
                store.read_rows,
                body,
                kind=partition.record_kind,
                symbol=partition.symbol,
                trade_date=partition.trade_date,
                row_count=partition.row_count,
                exchange_timezone=defaults["market_timezone"],
            )
        except store.RowsRejectedError as exc:
            await self._reject(result, run.run_id, exc.rule, partition, exc.detail)
            return

        outcome = await store.store_day(
            self.db,
            canonical_publisher(),
            store.DayDelivery(
                dataset_key=run.dataset_key,
                symbol=partition.symbol,
                trade_date=partition.trade_date,
                source=str(self.dataset_config.get("_ingest_source") or run.source),
                source_raw_sha256=payload.batch.source_raw_sha256,
                source_rows_sha256=partition.rows_sha256,
                fetched_at=ensure_utc(self.dataset_config["_ingest_fetched_at"]),
                run_id=run.run_id,
            ),
            table,
        )
        if outcome != "stale":
            result.success_records = 1
            return
        # Not an error and not a write: a newer fetch of this day is already stored.
        result.precedence_rejected_records = 1
        issue = DQIssueRecord(
            issue_type="INTRADAY_PARTITION_STALE",
            severity="warning",
            description="A newer fetch of this trading day is already stored",
            trade_date=datetime.combine(partition.trade_date, time.min, timezone.utc),
            raw_data={"symbol": partition.symbol, "record_kind": partition.record_kind},
        )
        await self.record_dq_issue(issue, run.run_id)
        result.dq_issues.append(issue)

    async def process(
        self, raw_payload: dict, run_id: UUID, *, commit: bool = True
    ) -> NormalizeResult:
        result = NormalizeResult(run_id=run_id, total_records=1)
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
