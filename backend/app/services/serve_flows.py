"""Serve reads of the TW exchanges' three institutional investors tables (三大法人).

Read only: rows are served as the exchanges published them. Only boards with an
active dataset are served; a board's tables exist only once its feed stored them.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.models.canonical import Instrument
from app.models.institutional_flow import (
    InstitutionalFlowPublication,
    InstitutionalFlowStock,
    InstitutionalFlowSummary,
)
from app.schemas.serve import (
    CursorPaginationInfo,
    InstitutionalAmountResponse,
    InstitutionalFlowListResponse,
    InstitutionalFlowResponse,
    InstitutionalFlowSides,
    InstitutionalFlowSummaryListResponse,
    InstitutionalFlowSummaryResponse,
    SortDirection,
)
from app.services.serve import (
    _date_text,
    _decode_cursor,
    _encode_cursor,
    _instrument_summary,
    _match_selectors,
    _require_matched,
    _validate_dates,
    active_flow_datasets,
)
from app.services.serve_errors import serve_error
from app.services.serve_selectors import parse_selectors

FlowSort = Literal[
    "symbol",
    "total_net",
    "foreign_ex_dealer_net",
    "investment_trust_net",
    "dealer_total_net",
]
MAX_SYMBOLS = 50
_SECURITY = re.compile(r"^[0-9A-Z]{4,6}$")
_FLOW_PAIRS = {("TW", "equity"), ("TW", "etf")}


def _boards(active: set[str], board: str | None) -> list[str]:
    if board is not None and board not in active:
        raise serve_error(
            422, "DATASET_NOT_AVAILABLE", f"Institutional tables are not active for {board}"
        )
    return [board] if board is not None else sorted(active)


def _symbols(raw: str) -> list[str]:
    values = [token.strip().upper() for token in raw.split(",")]
    if (
        not 1 <= len(values) <= MAX_SYMBOLS
        or len(set(values)) != len(values)
        or any(not _SECURITY.fullmatch(value) for value in values)
    ):
        raise serve_error(
            422,
            "INVALID_SYMBOLS",
            f"symbols must hold 1 to {MAX_SYMBOLS} distinct security codes such as 2330",
            details={"param": "symbols"},
        )
    return values


def _sides(row: InstitutionalFlowStock, prefix: str) -> InstitutionalFlowSides:
    return InstitutionalFlowSides(
        buy=getattr(row, f"{prefix}_buy", None),
        sell=getattr(row, f"{prefix}_sell", None),
        net=getattr(row, f"{prefix}_net", None),
    )


async def list_institutional_flows(
    db: AsyncSession,
    *,
    instruments_raw: str | None,
    symbols_raw: str | None,
    trade_date: date | None,
    board: str | None,
    start_date: date | None,
    end_date: date | None,
    sort: FlowSort,
    order: SortDirection,
    cursor: str | None,
    page_size: int,
) -> InstitutionalFlowListResponse:
    """Per-security flows: chosen securities over dates, or one day's whole table."""
    active = {scope.board for scope in await active_flow_datasets(db) if scope.kind == "stock"}
    if not active:
        raise serve_error(422, "DATASET_NOT_AVAILABLE", "Institutional flows are not active")
    if instruments_raw is not None and symbols_raw is not None:
        raise serve_error(422, "QUERY_SCOPE_REQUIRED", "Give instruments or symbols, not both")
    whole_table = instruments_raw is None and symbols_raw is None
    if whole_table and trade_date is None:
        raise serve_error(
            422,
            "QUERY_SCOPE_REQUIRED",
            "Give instruments, symbols, or a trade_date for its whole table",
        )
    if trade_date is not None and (start_date is not None or end_date is not None):
        raise serve_error(
            422, "INVALID_DATE_RANGE", "Give trade_date or start_date/end_date, not both"
        )
    if sort != "symbol" and trade_date is None:
        raise serve_error(422, "INVALID_SORT", "Sorting by a flow needs a single trade_date")
    _validate_dates(start_date, end_date)
    boards = _boards(active, board)
    symbols: list[str] | None = None
    if instruments_raw is not None:
        selectors = parse_selectors(instruments_raw, param="instruments", allow_uuid=True)
        matched = _require_matched(
            await _match_selectors(db, selectors, _FLOW_PAIRS), "Institutional flows"
        )
        symbols = sorted({instrument.symbol for instrument in matched})
    elif symbols_raw is not None:
        symbols = sorted(_symbols(symbols_raw))
    if trade_date is not None:
        start_date = end_date = trade_date
    scope = {
        "symbols": symbols,
        "boards": boards,
        "start_date": _date_text(start_date),
        "end_date": _date_text(end_date),
        "sort": sort,
        "order": order,
    }
    sort_column: Any = getattr(InstitutionalFlowStock, sort) if sort != "symbol" else None
    query = (
        select(InstitutionalFlowStock, InstitutionalFlowPublication, Instrument)
        .join(
            InstitutionalFlowPublication,
            InstitutionalFlowPublication.publication_id == InstitutionalFlowStock.publication_id,
        )
        .outerjoin(Instrument, Instrument.instrument_id == InstitutionalFlowStock.instrument_id)
        .where(InstitutionalFlowStock.board.in_(boards))
    )
    if symbols is not None:
        query = query.where(InstitutionalFlowStock.symbol.in_(symbols))
    if start_date is not None:
        query = query.where(InstitutionalFlowStock.trade_date >= start_date)
    if end_date is not None:
        query = query.where(InstitutionalFlowStock.trade_date <= end_date)
    ascending = order == "asc"
    if cursor:
        payload = _decode_cursor(cursor)
        if (
            set(payload) != {"v", "kind", "scope", "last"}
            or payload.get("v") != 1
            or payload.get("kind") != "institutional_flows"
            or payload.get("scope") != scope
        ):
            raise serve_error(422, "INVALID_CURSOR", "Cursor does not match this query")
        last = payload["last"]
        try:
            last_date = date.fromisoformat(last["trade_date"])
            last_board, last_symbol = str(last["board"]), str(last["symbol"])
            last_value = int(last["value"]) if sort_column is not None else None
        except (KeyError, TypeError, ValueError):
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid") from None
        query = query.where(
            _after(sort_column, ascending, last_date, last_board, last_symbol, last_value)
        )
    if sort_column is not None:
        value_order = sort_column.asc() if ascending else sort_column.desc()
        query = query.order_by(
            value_order, InstitutionalFlowStock.board, InstitutionalFlowStock.symbol
        )
    else:
        date_order = (
            InstitutionalFlowStock.trade_date.asc()
            if ascending
            else InstitutionalFlowStock.trade_date.desc()
        )
        query = query.order_by(
            date_order, InstitutionalFlowStock.board, InstitutionalFlowStock.symbol
        )
    rows = (await db.execute(query.limit(page_size + 1))).all()
    next_cursor = None
    if len(rows) > page_size:
        tail = rows[page_size - 1][0]
        next_cursor = _encode_cursor(
            {
                "v": 1,
                "kind": "institutional_flows",
                "scope": scope,
                "last": {
                    "trade_date": tail.trade_date.isoformat(),
                    "board": tail.board,
                    "symbol": tail.symbol,
                    **({"value": getattr(tail, sort)} if sort_column is not None else {}),
                },
            }
        )
        rows = rows[:page_size]
    return InstitutionalFlowListResponse(
        data=[
            InstitutionalFlowResponse(
                trade_date=row.trade_date,
                board=cast(Literal["TWSE", "TPEX"], row.board),
                symbol=row.symbol,
                name=row.name,
                instrument=_instrument_summary(instrument) if instrument is not None else None,
                foreign_ex_dealer=_sides(row, "foreign_ex_dealer"),
                foreign_dealer=_sides(row, "foreign_dealer"),
                foreign_total=_sides(row, "foreign_total"),
                investment_trust=_sides(row, "investment_trust"),
                dealer_proprietary=_sides(row, "dealer_proprietary"),
                dealer_hedge=_sides(row, "dealer_hedge"),
                dealer_total=_sides(row, "dealer_total"),
                total_net=row.total_net,
                source=publication.source,
                source_fetched_at=publication.source_fetched_at,
            )
            for row, publication, instrument in rows
        ],
        pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor),
    )


def _after(
    sort_column: Any,
    ascending: bool,
    last_date: date,
    last_board: str,
    last_symbol: str,
    last_value: int | None,
) -> ColumnElement[bool]:
    """Rows after the cursor's last row in the query's order."""
    tie = or_(
        InstitutionalFlowStock.board > last_board,
        and_(
            InstitutionalFlowStock.board == last_board, InstitutionalFlowStock.symbol > last_symbol
        ),
    )
    if sort_column is not None:
        beyond = sort_column > last_value if ascending else sort_column < last_value
        return or_(beyond, and_(sort_column == last_value, tie))
    later = (
        InstitutionalFlowStock.trade_date > last_date
        if ascending
        else InstitutionalFlowStock.trade_date < last_date
    )
    return or_(later, and_(InstitutionalFlowStock.trade_date == last_date, tie))


def _amount(
    rows: dict[str, InstitutionalFlowSummary], category: str
) -> InstitutionalAmountResponse:
    printed = rows.get(category)
    if printed is not None:
        return InstitutionalAmountResponse(
            buy_amount=int(printed.buy_amount),
            sell_amount=int(printed.sell_amount),
            net_amount=int(printed.net_amount),
            reported=True,
        )
    parts = {
        "foreign_total": ("foreign_ex_dealer", "foreign_dealer"),
        "dealer_total": ("dealer_proprietary", "dealer_hedge"),
    }[category]
    return InstitutionalAmountResponse(
        buy_amount=sum(int(rows[part].buy_amount) for part in parts),
        sell_amount=sum(int(rows[part].sell_amount) for part in parts),
        net_amount=sum(int(rows[part].net_amount) for part in parts),
        reported=False,
    )


async def list_institutional_flow_summary(
    db: AsyncSession,
    *,
    board: str | None,
    start_date: date | None,
    end_date: date | None,
    order: SortDirection,
    cursor: str | None,
    page_size: int,
) -> InstitutionalFlowSummaryListResponse:
    """Market totals in TWD by investor category, one row per board and day."""
    active = {scope.board for scope in await active_flow_datasets(db) if scope.kind == "summary"}
    if not active:
        raise serve_error(422, "DATASET_NOT_AVAILABLE", "Institutional totals are not active")
    _validate_dates(start_date, end_date)
    boards = _boards(active, board)
    scope = {
        "boards": boards,
        "start_date": _date_text(start_date),
        "end_date": _date_text(end_date),
        "order": order,
    }
    query = select(InstitutionalFlowPublication).where(
        InstitutionalFlowPublication.kind == "summary",
        InstitutionalFlowPublication.board.in_(boards),
    )
    if start_date is not None:
        query = query.where(InstitutionalFlowPublication.trade_date >= start_date)
    if end_date is not None:
        query = query.where(InstitutionalFlowPublication.trade_date <= end_date)
    ascending = order == "asc"
    if cursor:
        payload = _decode_cursor(cursor)
        if (
            set(payload) != {"v", "kind", "scope", "last_date", "last_board"}
            or payload.get("v") != 1
            or payload.get("kind") != "institutional_flow_summary"
            or payload.get("scope") != scope
        ):
            raise serve_error(422, "INVALID_CURSOR", "Cursor does not match this query")
        try:
            last_date = date.fromisoformat(payload["last_date"])
            last_board = str(payload["last_board"])
        except (TypeError, ValueError):
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid") from None
        query = query.where(
            or_(
                InstitutionalFlowPublication.trade_date > last_date
                if ascending
                else InstitutionalFlowPublication.trade_date < last_date,
                and_(
                    InstitutionalFlowPublication.trade_date == last_date,
                    InstitutionalFlowPublication.board > last_board,
                ),
            )
        )
    date_order = (
        InstitutionalFlowPublication.trade_date.asc()
        if ascending
        else InstitutionalFlowPublication.trade_date.desc()
    )
    publications = list(
        (
            await db.scalars(
                query.order_by(date_order, InstitutionalFlowPublication.board).limit(page_size + 1)
            )
        ).all()
    )
    next_cursor = None
    if len(publications) > page_size:
        tail = publications[page_size - 1]
        next_cursor = _encode_cursor(
            {
                "v": 1,
                "kind": "institutional_flow_summary",
                "scope": scope,
                "last_date": tail.trade_date.isoformat(),
                "last_board": tail.board,
            }
        )
        publications = publications[:page_size]
    ids: list[UUID] = [publication.publication_id for publication in publications]
    lines: dict[UUID, dict[str, InstitutionalFlowSummary]] = {}
    if ids:
        for line in await db.scalars(
            select(InstitutionalFlowSummary).where(InstitutionalFlowSummary.publication_id.in_(ids))
        ):
            lines.setdefault(line.publication_id, {})[line.category] = line
    data = []
    for publication in publications:
        rows = lines.get(publication.publication_id, {})
        data.append(
            InstitutionalFlowSummaryResponse(
                trade_date=publication.trade_date,
                board=cast(Literal["TWSE", "TPEX"], publication.board),
                foreign_ex_dealer=_amount(rows, "foreign_ex_dealer"),
                foreign_dealer=_amount(rows, "foreign_dealer"),
                foreign_total=_amount(rows, "foreign_total"),
                investment_trust=_amount(rows, "investment_trust"),
                dealer_proprietary=_amount(rows, "dealer_proprietary"),
                dealer_hedge=_amount(rows, "dealer_hedge"),
                dealer_total=_amount(rows, "dealer_total"),
                total=_amount(rows, "total"),
                source=publication.source,
                source_fetched_at=publication.source_fetched_at,
            )
        )
    return InstitutionalFlowSummaryListResponse(
        data=data, pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor)
    )
