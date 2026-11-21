"""Normalizer for ``exchange_daily_quote.v1``: the exchange's own daily quotation."""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert

from app.models.canonical import ExchangeDailyQuote, ExchangeReportArchive
from app.models.registry import IngestionRun
from app.schemas.ingress import ExchangeDailyQuotePayload, ExchangeDailyQuoteRow
from app.services.dq.validators import DQIssueRecord
from app.services.exchange_reports import ReportRejectedError, archive_report
from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.normalize.contracts import _contract_context
from app.services.normalize.intraday import canonical_publisher, raw_reader
from app.services.object_storage import ObjectIntegrityError, ObjectNotFoundError
from app.utils import utc_now
from app.utils.datetime_utils import ensure_utc


def _day(row: ExchangeDailyQuoteRow) -> datetime:
    return datetime.combine(row.trade_date, time.min, timezone.utc)


def quote_issues(row: ExchangeDailyQuoteRow, raw: dict[str, Any]) -> list[DQIssueRecord]:
    """Rules the published values must keep; only contradictions block the write.

    The close is not checked against high/low: a non-auction close is the median
    of nominal prices.  The average traded price may leave high/low too, since
    odd-lot and special-lot trades are outside them, so that is a warning.
    """
    issues: list[DQIssueRecord] = []

    def issue(issue_type: str, severity: str, description: str) -> None:
        issues.append(DQIssueRecord(issue_type, severity, description, _day(row), raw))

    if row.high is not None and row.low is not None and row.high < row.low:
        issue("QUOTE_HIGH_BELOW_LOW", "error", "High is below low")
    if row.volume == 0 and row.turnover != 0:
        issue("QUOTE_TURNOVER_WITHOUT_VOLUME", "error", "Turnover is reported with no volume")
    if row.volume > 0 and row.high is not None and row.low is not None:
        average = row.turnover / Decimal(row.volume)
        # Turnover is published in whole currency units.
        tolerance = Decimal(1) / Decimal(row.volume)
        if average < row.low - tolerance or average > row.high + tolerance:
            issue(
                "QUOTE_AVERAGE_PRICE_OUTSIDE_RANGE",
                "warning",
                "Turnover divided by volume is outside high/low",
            )
    return issues


class ExchangeDailyQuoteNormalizer(BaseNormalizer):
    """Archive the cited report once, then store each line read from it.

    A report that cannot be proven fails every line of the delivery with a DQ
    error and stores nothing; a storage failure is raised for the queue to
    retry.  A line from an older fetch never replaces a newer one.
    """

    def map_fields(self, raw_data: dict) -> list[Any]:
        values = raw_data.get("data", [])
        return values if isinstance(values, list) else []

    async def _reject_all(
        self, result: NormalizeResult, run_id: UUID, rule: str, detail: dict | None = None
    ) -> None:
        issue = DQIssueRecord(
            issue_type="EXCHANGE_REPORT_REJECTED",
            severity="error",
            description=f"Exchange report rejected: {rule}",
            raw_data={"rule": rule, **(detail or {})},
        )
        await self.record_dq_issue(issue, run_id)
        result.dq_issues.append(issue)
        result.failed_records = result.total_records
        result.error_message = issue.description

    async def _store_row(
        self,
        row: ExchangeDailyQuoteRow,
        *,
        archive: ExchangeReportArchive,
        run: IngestionRun,
        source: str,
        fetched_at: datetime,
    ) -> tuple[UUID, bool]:
        """Upsert one line; False when a newer fetch of it is already stored."""
        market, asset_class, _, _ = _contract_context(self.dataset_config)
        instrument = await self.get_or_create_instrument(
            row.symbol,
            name=row.name,
            currency=row.currency,
            market=market,
            asset_class=asset_class,
        )
        now = utc_now()
        values = {
            "board": archive.board,
            "currency": row.currency,
            "trading_status": row.trading_status,
            "previous_close": row.previous_close,
            "close": row.close,
            "ask": row.ask,
            "bid": row.bid,
            "high": row.high,
            "low": row.low,
            "volume": row.volume,
            "turnover": row.turnover,
            "source": source,
            "source_fetched_at": fetched_at,
            "archive_id": archive.archive_id,
            "run_id": run.run_id,
            "updated_at": now,
        }
        statement = insert(ExchangeDailyQuote).values(
            instrument_id=instrument.instrument_id,
            trade_date=row.trade_date,
            created_at=now,
            **values,
        )
        applied = await self.db.scalar(
            statement.on_conflict_do_update(
                index_elements=["instrument_id", "trade_date"],
                set_={key: getattr(statement.excluded, key) for key in values},
                where=ExchangeDailyQuote.source_fetched_at <= statement.excluded.source_fetched_at,
            ).returning(ExchangeDailyQuote.instrument_id)
        )
        return instrument.instrument_id, applied is not None

    async def _store(self, raw_payload: dict, run: IngestionRun, result: NormalizeResult) -> None:
        try:
            payload = ExchangeDailyQuotePayload.model_validate(raw_payload)
        except ValueError:
            await self._reject_all(result, run.run_id, "payload")
            return
        source = str(self.dataset_config.get("_ingest_source") or run.source)
        try:
            archive = await archive_report(
                self.db,
                raw_reader(),
                canonical_publisher(),
                source=source,
                batch=payload.batch,
                run_id=run.run_id,
            )
        except ReportRejectedError as exc:
            await self._reject_all(result, run.run_id, exc.rule)
            return
        except (ObjectNotFoundError, ObjectIntegrityError) as exc:
            await self._reject_all(result, run.run_id, "raw_object", {"reason": str(exc)})
            return
        fetched_at = ensure_utc(self.dataset_config["_ingest_fetched_at"])
        for row, raw in zip(payload.data, raw_payload["data"], strict=True):
            issues = quote_issues(row, raw)
            if any(issue.severity == "error" for issue in issues):
                for issue in issues:
                    await self.record_dq_issue(issue, run.run_id)
                    result.dq_issues.append(issue)
                result.failed_records += 1
                continue
            instrument_id, applied = await self._store_row(
                row, archive=archive, run=run, source=source, fetched_at=fetched_at
            )
            for issue in issues:
                await self.record_dq_issue(issue, run.run_id, instrument_id)
                result.dq_issues.append(issue)
            if applied:
                result.success_records += 1
            else:
                result.precedence_rejected_records += 1

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
            await self.record_precedence_rejection_summary(result, run_id)
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
