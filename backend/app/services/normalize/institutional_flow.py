"""Normalizers for the exchanges' three institutional investors tables (三大法人).

A table is all or nothing. Every printed relation is checked first: each net is
buy minus sell, the total is the four categories the exchanges add up (foreign
dealers are already counted under dealers), and printed subtotals equal their
parts. Any violation rejects the whole table as a DQ error and stores nothing,
so the table is fetched again. A table is replaced as a whole by a later fetch;
an earlier fetch than the stored one is a precedence rejection.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import Instrument
from app.models.institutional_flow import (
    InstitutionalFlowPublication,
    InstitutionalFlowStock,
    InstitutionalFlowSummary,
)
from app.models.registry import IngestionRun
from app.schemas.ingress import (
    INSTITUTIONAL_SECURITY_PATTERN,
    INSTITUTIONAL_STOCK_COLUMNS,
    InstitutionalFlowStockPayload,
    InstitutionalFlowSummaryPayload,
)
from app.services.dq.validators import DQIssueRecord
from app.services.normalize.base import BaseNormalizer, NormalizeResult
from app.services.object_storage import ObjectIntegrityError, ObjectNotFoundError
from app.utils import utc_now, uuid7
from app.utils.datetime_utils import ensure_utc

# DQ issue types; a rejected table is one invalid member (the board) of its day.
TABLE_REJECTED = "INSTITUTIONAL_TABLE_REJECTED"
TABLE_STALE = "INSTITUTIONAL_TABLE_STALE"
INSTRUMENT_AMBIGUOUS = "INSTITUTIONAL_INSTRUMENT_AMBIGUOUS"
STOCK_COLUMN_NAMES = tuple(column.name for column in INSTITUTIONAL_STOCK_COLUMNS)
_NULLABLE = {column.name for column in INSTITUTIONAL_STOCK_COLUMNS if column.nullable}
_SIDES = ("buy", "sell", "net")
_ROW_LIMIT_FOR_DETAIL = 20


class TableRejectedError(ValueError):
    """The table breaks a rule; ``rule`` and ``detail`` are bounded diagnostics."""

    def __init__(self, rule: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(rule)
        self.rule = rule
        self.detail = dict(detail or {})


def _check_flow(values: Mapping[str, int | None], prefix: str, where: str) -> None:
    buy, sell, net = (values.get(f"{prefix}_{side}") for side in _SIDES)
    if buy is not None and sell is not None and net is not None and buy - sell != net:
        raise TableRejectedError("net_is_buy_minus_sell", {"at": where, "category": prefix})


def _check_sum(
    values: Mapping[str, int | None], total: str, parts: Iterable[str], where: str, side: str
) -> None:
    value = values.get(f"{total}_{side}")
    addends = [values.get(f"{part}_{side}") for part in parts]
    if value is None or any(addend is None for addend in addends):
        return
    if value != sum(addend for addend in addends if addend is not None):
        raise TableRejectedError(f"{total}_is_sum", {"at": where, "side": side})


def check_identities(values: Mapping[str, int | None], where: str) -> None:
    """The relations the exchanges print, for one summary or one security."""
    for prefix in (
        "foreign_ex_dealer",
        "foreign_dealer",
        "foreign_total",
        "investment_trust",
        "dealer_proprietary",
        "dealer_hedge",
        "dealer_total",
        "total",
    ):
        _check_flow(values, prefix, where)
    for side in _SIDES:
        _check_sum(values, "dealer_total", ("dealer_proprietary", "dealer_hedge"), where, side)
        _check_sum(values, "foreign_total", ("foreign_ex_dealer", "foreign_dealer"), where, side)
        _check_sum(
            values,
            "total",
            ("foreign_ex_dealer", "investment_trust", "dealer_proprietary", "dealer_hedge"),
            where,
            side,
        )


def read_stock_rows(body: bytes, *, row_count: int) -> list[dict[str, Any]]:
    """The rows object as published by the layout, checked row by row."""
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise TableRejectedError("rows_object_json") from None
    if not isinstance(document, dict) or set(document) != {"columns", "rows"}:
        raise TableRejectedError("rows_object_shape")
    if document["columns"] != list(STOCK_COLUMN_NAMES):
        raise TableRejectedError("rows_object_layout")
    rows = document["rows"]
    if not isinstance(rows, list) or len(rows) != row_count:
        raise TableRejectedError("row_count", {"declared": row_count})
    pattern = re.compile(INSTITUTIONAL_SECURITY_PATTERN)
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(STOCK_COLUMN_NAMES):
            raise TableRejectedError("row_shape", {"row": position})
        values = dict(zip(STOCK_COLUMN_NAMES, row, strict=True))
        symbol = values["symbol"]
        if not isinstance(symbol, str) or not pattern.fullmatch(symbol):
            raise TableRejectedError("security_code", {"row": position})
        if symbol in seen:
            raise TableRejectedError("security_repeated", {"security": symbol})
        seen.add(symbol)
        name = values["name"]
        if name is not None and (not isinstance(name, str) or len(name) > 100):
            raise TableRejectedError("security_name", {"security": symbol})
        for column in STOCK_COLUMN_NAMES[2:]:
            value = values[column]
            if value is None and column in _NULLABLE:
                continue
            if type(value) is not int or abs(value) > 10**18:
                raise TableRejectedError("value_type", {"security": symbol, "column": column})
            if not column.endswith("_net") and value < 0:
                raise TableRejectedError("negative_volume", {"security": symbol, "column": column})
        check_identities(values, symbol)
        parsed.append(values)
    return parsed


class _InstitutionalTableNormalizer(BaseNormalizer):
    kind = ""

    def map_fields(self, raw_data: dict) -> list[Any]:
        values = raw_data.get("data", [])
        return values if isinstance(values, list) else []

    async def _issue(
        self,
        result: NormalizeResult,
        run_id: UUID,
        *,
        issue_type: str,
        severity: str,
        description: str,
        board: str | None,
        trade_date: date | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        issue = DQIssueRecord(
            issue_type=issue_type,
            severity=severity,
            description=description,
            trade_date=(
                datetime.combine(trade_date, time.min, timezone.utc) if trade_date else None
            ),
            # ``symbol`` is the plan member, so a rejected table settles that member.
            raw_data={**(detail or {}), "symbol": board, "table": self.kind},
        )
        await self.record_dq_issue(issue, run_id)
        result.dq_issues.append(issue)

    async def _reject(
        self,
        result: NormalizeResult,
        run_id: UUID,
        rule: str,
        board: str | None,
        trade_date: date | None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        await self._issue(
            result,
            run_id,
            issue_type=TABLE_REJECTED,
            severity="error",
            description=f"Institutional {self.kind} table rejected: {rule}",
            board=board,
            trade_date=trade_date,
            detail={"rule": rule, **(detail or {})},
        )
        result.failed_records = 1
        result.error_message = f"Institutional {self.kind} table rejected: {rule}"

    async def _publish(
        self,
        run: IngestionRun,
        *,
        board: str,
        trade_date: date,
        source_symbol: str | None,
        source_raw_sha256: str,
        rows_sha256: str | None,
        row_count: int,
    ) -> UUID | None:
        """Claim the table's stored version for this fetch; None when a newer one is kept."""
        fetched_at = ensure_utc(self.dataset_config["_ingest_fetched_at"])
        source = str(self.dataset_config.get("_ingest_source") or run.source)
        stored = await self.db.scalar(
            select(InstitutionalFlowPublication)
            .where(
                InstitutionalFlowPublication.kind == self.kind,
                InstitutionalFlowPublication.board == board,
                InstitutionalFlowPublication.trade_date == trade_date,
            )
            .with_for_update()
        )
        now = utc_now()
        if stored is None:
            stored = InstitutionalFlowPublication(
                publication_id=uuid7(),
                kind=self.kind,
                board=board,
                trade_date=trade_date,
                created_at=now,
            )
            self.db.add(stored)
        elif ensure_utc(stored.source_fetched_at) > fetched_at:
            return None
        stored.dataset_key = run.dataset_key
        stored.source = source
        stored.source_symbol = source_symbol
        stored.source_fetched_at = fetched_at
        stored.source_raw_sha256 = source_raw_sha256
        stored.rows_sha256 = rows_sha256
        stored.row_count = row_count
        stored.run_id = run.run_id
        stored.updated_at = now
        await self.db.flush()
        return stored.publication_id

    async def _stale(
        self, result: NormalizeResult, run_id: UUID, board: str, trade_date: date
    ) -> None:
        # Not an error and not a write: a newer fetch of this table is already stored.
        result.precedence_rejected_records = 1
        await self._issue(
            result,
            run_id,
            issue_type=TABLE_STALE,
            severity="warning",
            description="A newer fetch of this table is already stored",
            board=board,
            trade_date=trade_date,
        )

    async def _store(self, raw_payload: dict, run: IngestionRun, result: NormalizeResult) -> None:
        raise NotImplementedError

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


class InstitutionalFlowSummaryNormalizer(_InstitutionalTableNormalizer):
    """``institutional_flow_summary.v1``: a board's TWD totals of one day."""

    kind = "summary"

    async def _store(self, raw_payload: dict, run: IngestionRun, result: NormalizeResult) -> None:
        try:
            payload = InstitutionalFlowSummaryPayload.model_validate(raw_payload)
        except ValueError:
            await self._reject(result, run.run_id, "payload", None, None)
            return
        table = payload.data[0]
        values: dict[str, int | None] = {}
        for item in table.categories:
            values.update(
                {
                    f"{item.category}_buy": item.buy_amount,
                    f"{item.category}_sell": item.sell_amount,
                    f"{item.category}_net": item.net_amount,
                }
            )
        try:
            check_identities(values, table.symbol)
        except TableRejectedError as exc:
            await self._reject(
                result, run.run_id, exc.rule, table.symbol, table.trade_date, exc.detail
            )
            return
        publication_id = await self._publish(
            run,
            board=table.symbol,
            trade_date=table.trade_date,
            source_symbol=table.source_symbol,
            source_raw_sha256=payload.batch.source_raw_sha256,
            rows_sha256=None,
            row_count=len(table.categories),
        )
        if publication_id is None:
            await self._stale(result, run.run_id, table.symbol, table.trade_date)
            return
        await self.db.execute(
            delete(InstitutionalFlowSummary).where(
                InstitutionalFlowSummary.board == table.symbol,
                InstitutionalFlowSummary.trade_date == table.trade_date,
            )
        )
        await self.db.execute(
            insert(InstitutionalFlowSummary),
            [
                {
                    "trade_date": table.trade_date,
                    "board": table.symbol,
                    "category": item.category,
                    "publication_id": publication_id,
                    "buy_amount": Decimal(item.buy_amount),
                    "sell_amount": Decimal(item.sell_amount),
                    "net_amount": Decimal(item.net_amount),
                    "currency": "TWD",
                }
                for item in table.categories
            ],
        )
        result.success_records = 1


async def resolve_tw_instruments(db: AsyncSession, symbols: Iterable[str]) -> dict[str, UUID]:
    """TW equity or ETF instruments by symbol; a symbol listed as both stays unresolved."""
    wanted = sorted(set(symbols))
    found: dict[str, list[UUID]] = {}
    for start in range(0, len(wanted), 5000):
        rows = await db.execute(
            select(Instrument.symbol, Instrument.instrument_id).where(
                Instrument.market == "TW",
                Instrument.asset_class.in_(("equity", "etf")),
                Instrument.symbol.in_(wanted[start : start + 5000]),
            )
        )
        for symbol, instrument_id in rows:
            found.setdefault(symbol, []).append(instrument_id)
    return {symbol: ids[0] for symbol, ids in found.items() if len(ids) == 1}


class InstitutionalFlowStockNormalizer(_InstitutionalTableNormalizer):
    """``institutional_flow_stock.v1``: a board's per-security flows of one day, in shares."""

    kind = "stock"

    async def _store(self, raw_payload: dict, run: IngestionRun, result: NormalizeResult) -> None:
        try:
            payload = InstitutionalFlowStockPayload.model_validate(raw_payload)
        except ValueError:
            await self._reject(result, run.run_id, "payload", None, None)
            return
        table = payload.data[0]
        from app.services.normalize.intraday import raw_reader

        try:
            body = await asyncio.to_thread(
                raw_reader().read,
                table.rows_ref,
                sha256=table.rows_sha256,
                max_bytes=table.rows_bytes,
            )
        except (ObjectNotFoundError, ObjectIntegrityError) as exc:
            await self._reject(
                result,
                run.run_id,
                "rows_object",
                table.symbol,
                table.trade_date,
                {"reason": str(exc)[:200]},
            )
            return
        try:
            rows = read_stock_rows(body, row_count=table.row_count)
        except TableRejectedError as exc:
            await self._reject(
                result, run.run_id, exc.rule, table.symbol, table.trade_date, exc.detail
            )
            return
        publication_id = await self._publish(
            run,
            board=table.symbol,
            trade_date=table.trade_date,
            source_symbol=table.source_symbol,
            source_raw_sha256=payload.batch.source_raw_sha256,
            rows_sha256=table.rows_sha256,
            row_count=table.row_count,
        )
        if publication_id is None:
            await self._stale(result, run.run_id, table.symbol, table.trade_date)
            return
        instruments = await resolve_tw_instruments(self.db, (row["symbol"] for row in rows))
        ambiguous = await self._ambiguous(rows, instruments)
        if ambiguous:
            await self._issue(
                result,
                run.run_id,
                issue_type=INSTRUMENT_AMBIGUOUS,
                severity="warning",
                description="Securities listed as both a TW equity and an ETF are stored unlinked",
                board=table.symbol,
                trade_date=table.trade_date,
                detail={"securities": ambiguous[:_ROW_LIMIT_FOR_DETAIL], "count": len(ambiguous)},
            )
        await self.db.execute(
            delete(InstitutionalFlowStock).where(
                InstitutionalFlowStock.board == table.symbol,
                InstitutionalFlowStock.trade_date == table.trade_date,
            )
        )
        values = [
            {
                **row,
                "trade_date": table.trade_date,
                "board": table.symbol,
                "publication_id": publication_id,
                "instrument_id": instruments.get(row["symbol"]),
            }
            for row in rows
        ]
        for start in range(0, len(values), 2000):
            await self.db.execute(insert(InstitutionalFlowStock), values[start : start + 2000])
        result.success_records = 1

    async def _ambiguous(self, rows: list[dict[str, Any]], resolved: Mapping[str, UUID]) -> list:
        unresolved = [row["symbol"] for row in rows if row["symbol"] not in resolved]
        if not unresolved:
            return []
        counts = await self.db.execute(
            select(Instrument.symbol)
            .where(
                Instrument.market == "TW",
                Instrument.asset_class.in_(("equity", "etf")),
                Instrument.symbol.in_(unresolved),
            )
            .group_by(Instrument.symbol)
        )
        return sorted(symbol for (symbol,) in counts)
