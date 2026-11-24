"""Thin HTTP routes for canonical read-only Serve queries.

Each route publishes a stable ``operation_id`` and English descriptions: clients
generate LLM tool definitions from this OpenAPI document, so the operation id
becomes the tool name and the descriptions become what the agent reads.
"""

from datetime import date, time
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    require_intraday_serve_api_key,
    require_serve_api_key,
    verify_paginated_serve_api_key,
    verify_serve_api_key,
)
from app.dependencies import get_db
from app.schemas.serve import (
    CalendarListResponse,
    CalendarResponse,
    DatasetListResponse,
    EODLatestResponse,
    EODListResponse,
    ExchangeQuoteListResponse,
    FuturesContractListResponse,
    FuturesEODListResponse,
    InstrumentDetailResponse,
    InstrumentListResponse,
    InstrumentResolveResponse,
    InstrumentSort,
    IntradayBarListResponse,
    IntradayDownloadManifestResponse,
    IntradayInterval,
    IntradayTickListResponse,
    MarketFreshnessSummaryListResponse,
    MarketFreshnessSummaryResponse,
    MinuteListResponse,
    PublishedCalendarYearEnvelope,
    PublishedCalendarYearResponse,
    ServeErrorResponse,
    SortDirection,
)
from app.services import serve as serve_queries
from app.services import serve_downloads, serve_intraday
from app.services.effective_calendar import published_calendar_year
from app.services.market_freshness import list_market_freshness
from app.services.serve_downloads import DOWNLOAD_SCOPE
from app.services.serve_errors import serve_error
from app.services.slot_identity import CanonicalSlotId

router = APIRouter()

ResponseMap = dict[int | str, dict[str, Any]]


def _errors(
    extra: dict[int, str] | None = None, *, paginated: bool = False, key_required: bool = False
) -> ResponseMap:
    """Document the coded errors a route can return; every body is ``ServeErrorResponse``."""
    descriptions: dict[int, str] = {
        401: (
            "`API_KEY_REQUIRED`: send a Serve API key in the `X-API-Key` header"
            + ("." if key_required else " (required when the deployment enforces Serve auth).")
            + " `API_KEY_INVALID`: the key is unknown, revoked, expired or not a Serve key."
        ),
        403: "`SCOPE_REQUIRED`: the key lacks the scope named in `details.scope`.",
        422: (
            "`INVALID_PARAMETER`: a parameter is malformed or out of range; `details.errors` "
            "lists each one."
        ),
        429: (
            "`RATE_LIMITED`: the key's request allowance is used up; retry after the "
            "`Retry-After` header (seconds)."
        ),
    }
    if paginated:
        descriptions[400] = (
            "`PAGE_SIZE_EXCEEDS_KEY_LIMIT`: `page_size` is above the key's limit "
            "(`details.limit`); retry with a smaller `page_size`."
        )
    for status, text in (extra or {}).items():
        descriptions[status] = f"{descriptions[status]} {text}" if status in descriptions else text
    return {
        status: {"model": ServeErrorResponse, "description": text}
        for status, text in sorted(descriptions.items())
    }


INSTRUMENT_NOT_FOUND = "`INSTRUMENT_NOT_FOUND`: no instrument has this id."
DATASET_NOT_AVAILABLE = (
    "`DATASET_NOT_AVAILABLE`: the instrument or market exists but has no data of this kind."
)
INVALID_DATE_RANGE = "`INVALID_DATE_RANGE`: dates are reversed or the range is too long."
INVALID_CURSOR = "`INVALID_CURSOR`: the cursor is malformed or was issued for other parameters."
INTRADAY_UNAVAILABLE = (
    "`INTRADAY_READS_BUSY`, `INTRADAY_DATA_CHANGING` or `INTRADAY_STORAGE_UNAVAILABLE`: "
    "temporary; retry later (honour `Retry-After` when present)."
)
CONFIGURATION_INVALID = (
    "`SERVE_CONFIGURATION_INVALID`: the dataset catalog is misconfigured; retry later."
)
CALENDAR_MARKETS = "`TW`, `US`, `HK`, `CN` or `TAIFEX`"

InstrumentIdQuery = Annotated[
    UUID,
    Query(
        description=(
            "FinDB instrument UUID. Find it with `list_instruments` (search by symbol or name)."
        ),
        examples=["0190a5c8-3f1e-7c2a-9b4d-5e6f7a8b9c0d"],
    ),
]
StartDateQuery = Annotated[
    date | None,
    Query(
        description="First exchange-local trade date to include (YYYY-MM-DD).",
        examples=["2026-11-02"],
    ),
]
EndDateQuery = Annotated[
    date | None,
    Query(
        description="Last exchange-local trade date to include (YYYY-MM-DD).",
        examples=["2026-11-20"],
    ),
]
CursorQuery = Annotated[
    str | None,
    Query(
        description=(
            "Opaque `pagination.next_cursor` from the previous page. Omit for the first page; "
            "keep every other parameter unchanged while paging."
        )
    ),
]


def _page_size(default: int, maximum: int) -> Any:
    return Query(
        ge=1,
        le=maximum,
        description=(
            f"Rows per page, 1-{maximum} (default {default}). The API key may set a lower limit."
        ),
    )


@router.get(
    "/datasets",
    response_model=DatasetListResponse,
    operation_id="list_datasets",
    summary="List the datasets FinDB serves",
    description=(
        "Returns every active dataset with its market, asset class, frequency, the endpoint "
        "that serves it (`data_kind`) and the stored date range. Call this first to learn "
        "which markets and data types exist."
    ),
    responses=_errors({503: CONFIGURATION_INVALID}),
)
async def get_datasets(
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> DatasetListResponse:
    return await serve_queries.list_datasets(db)


@router.get(
    "/instruments",
    response_model=InstrumentListResponse,
    operation_id="list_instruments",
    summary="Search instruments",
    description=(
        "Searches the instruments FinDB holds data for. `q` matches part of the symbol, name "
        "or instrument id; results are ranked so an exact symbol comes first. When you already "
        "know the market and symbol, use `resolve_instruments` instead. Each result's "
        "`coverage` shows the stored date range per endpoint. Uses page-number pagination with "
        "totals."
    ),
    responses=_errors({503: CONFIGURATION_INVALID}, paginated=True),
)
async def get_instruments(
    q: Annotated[
        str | None,
        Query(
            max_length=200,
            description=(
                "Text to find in the symbol or name (case-insensitive), or a whole "
                "`instrument_id`. Results rank exact symbol, symbol prefix, name prefix, "
                "symbol substring, then name substring, unless `sort_by` is given."
            ),
            examples=["2330", "Apple"],
        ),
    ] = None,
    market: Annotated[
        str | None, Query(description="Only this market, e.g. `TW`, `US`, `HK`.", examples=["TW"])
    ] = None,
    asset_class: Annotated[
        str | None,
        Query(
            description="Only this asset class, e.g. `equity`, `etf`, `index`, `future`.",
            examples=["equity"],
        ),
    ] = None,
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Only this listing status, e.g. `active`."),
    ] = None,
    has_eod: Annotated[
        bool | None,
        Query(description="true: only instruments with daily bars; false: only those without."),
    ] = None,
    has_minute: Annotated[
        bool | None,
        Query(description="true: only instruments with legacy minute bars; false: without."),
    ] = None,
    sort_by: Annotated[
        InstrumentSort | None,
        Query(
            description=(
                "Sort field. Default: `relevance` when `q` is given (exact symbol, symbol "
                "prefix, name prefix, symbol substring, then the rest; shorter symbols first), "
                "otherwise `market`."
            )
        ),
    ] = None,
    sort_dir: Annotated[SortDirection, Query(description="Sort direction.")] = "asc",
    page: Annotated[int, Query(ge=1, description="Page number, starting at 1.")] = 1,
    page_size: Annotated[int, _page_size(50, 100)] = 50,
    _: str | None = Depends(verify_paginated_serve_api_key(50)),
    db: AsyncSession = Depends(get_db),
) -> InstrumentListResponse:
    return await serve_queries.list_instruments(
        db,
        q=q,
        market=market,
        asset_class=asset_class,
        status=status_filter,
        has_eod=has_eod,
        has_minute=has_minute,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/instruments/resolve",
    response_model=InstrumentResolveResponse,
    operation_id="resolve_instruments",
    summary="Find instruments by exact market and symbol",
    description=(
        "Turns up to 50 `MARKET:SYMBOL` values (e.g. `TW:2330,US:AAPL,HK:700`) into FinDB "
        "instruments in one call, with each one's `instrument_id` and stored date ranges. "
        "Matching is exact after normalization: case-insensitive, and HK numeric codes are "
        "zero-padded to five digits. Each input gets its own `status`, so unknown symbols do "
        "not fail the request. Use this before the data endpoints whenever the symbol is known."
    ),
    responses=_errors(
        {
            422: (
                "`INVALID_SYMBOLS`: `symbols` is empty, has more than 50 values, or a value "
                "lacks a market."
            ),
            503: CONFIGURATION_INVALID,
        }
    ),
)
async def resolve_instruments(
    symbols: Annotated[
        str,
        Query(
            description=(
                "Comma-separated `MARKET:SYMBOL` values, at most 50. A value without `MARKET:` "
                "uses `market`."
            ),
            examples=["TW:2330,US:AAPL,HK:700"],
        ),
    ],
    market: Annotated[
        str | None,
        Query(description="Market for values given without `MARKET:`, e.g. `TW`."),
    ] = None,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> InstrumentResolveResponse:
    return await serve_queries.resolve_instruments(db, symbols_raw=symbols, market=market)


@router.get(
    "/instruments/{instrument_id}",
    response_model=InstrumentDetailResponse,
    operation_id="get_instrument",
    summary="Get one instrument",
    description="Returns one instrument and the stored date range of each kind of data it has.",
    responses=_errors({404: INSTRUMENT_NOT_FOUND, 503: CONFIGURATION_INVALID}),
)
async def get_instrument(
    instrument_id: Annotated[UUID, Path(description="FinDB instrument UUID.")],
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> InstrumentDetailResponse:
    return await serve_queries.get_instrument(db, instrument_id)


InstrumentsQuery = Annotated[
    str,
    Query(
        description=(
            "Comma-separated instruments, at most 50, each a FinDB UUID or `MARKET:SYMBOL` "
            "(markets may differ). Symbols are matched like `resolve_instruments`."
        ),
        examples=["TW:2330,US:AAPL,HK:700"],
    ),
]
ORDER_DESCRIPTION = "`desc` (default): newest trade date first; `asc`: oldest first."
SELECTOR_ERRORS = (
    "`AMBIGUOUS_INSTRUMENT`: a `MARKET:SYMBOL` matches several asset classes; "
    "`details.matches` lists the candidate ids to use instead. `INVALID_SYMBOLS`: a value is "
    "neither a UUID nor `MARKET:SYMBOL`, or there are more than 50."
)


@router.get(
    "/eod",
    response_model=EODListResponse,
    operation_id="list_eod",
    summary="Daily OHLCV bars",
    description=(
        "Daily bars for equities, ETFs and indices. Give exactly one scope: `instruments` (up "
        "to 50, may mix markets) or `market` for every instrument of one market. Rows are "
        "ordered by trade date (`order`), then instrument. Prices are as traded (not adjusted "
        "for splits or dividends); `units` gives the volume unit per asset class. Dates are "
        "exchange-local and inclusive; without dates every stored date is returned, so give a "
        "range and follow `pagination.next_cursor` until it is null. For just the latest bar, "
        "use `get_latest_eod`."
    ),
    responses=_errors(
        {
            404: f"{INSTRUMENT_NOT_FOUND} `details.inputs` lists the unknown values.",
            422: (
                "`QUERY_SCOPE_REQUIRED`: give `instruments` or `market`, not both. "
                f"{SELECTOR_ERRORS} {DATASET_NOT_AVAILABLE} {INVALID_DATE_RANGE} {INVALID_CURSOR}"
            ),
            503: CONFIGURATION_INVALID,
        },
        paginated=True,
    ),
)
async def get_eod(
    instruments: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated instruments, at most 50, each a FinDB UUID or `MARKET:SYMBOL`. "
                "Do not combine with `market`."
            ),
            examples=["TW:2330,US:AAPL"],
        ),
    ] = None,
    market: Annotated[
        str | None,
        Query(description="Every instrument of this market, e.g. `TW`.", examples=["TW"]),
    ] = None,
    start_date: StartDateQuery = None,
    end_date: EndDateQuery = None,
    order: Annotated[SortDirection, Query(description=ORDER_DESCRIPTION)] = "desc",
    cursor: CursorQuery = None,
    page_size: Annotated[int, _page_size(100, 1000)] = 100,
    _: str | None = Depends(verify_paginated_serve_api_key(100)),
    db: AsyncSession = Depends(get_db),
) -> EODListResponse:
    return await serve_queries.list_eod(
        db,
        instruments_raw=instruments,
        market=market,
        start_date=start_date,
        end_date=end_date,
        order=order,
        cursor=cursor,
        page_size=page_size,
    )


@router.get(
    "/eod/latest",
    response_model=EODLatestResponse,
    operation_id="get_latest_eod",
    summary="Latest daily close of several instruments",
    description=(
        "For up to 50 instruments (may mix markets), the latest stored daily bar on or before "
        "`as_of_date`, with the previous bar's close and the change in price and percent. "
        "The change is reported only when the previous bar is the prior session of the market "
        "calendar (`missing_sessions` is 0). "
        "Inputs that cannot be answered are listed in `missing` with a reason instead of "
        "failing the request. Check `trade_date`: a market's latest bar may be from an earlier "
        "day than today (see `list_market_freshness`)."
    ),
    responses=_errors({422: "`INVALID_SYMBOLS`: a value is malformed or there are more than 50."}),
)
async def get_latest_eod(
    instruments: InstrumentsQuery,
    as_of_date: Annotated[
        date | None,
        Query(
            description=(
                "Latest exchange-local trade date to consider (YYYY-MM-DD); default: the latest "
                "stored. On a holiday the bar before it is returned."
            ),
            examples=["2026-11-20"],
        ),
    ] = None,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> EODLatestResponse:
    return await serve_queries.latest_eod(db, instruments_raw=instruments, as_of_date=as_of_date)


@router.get(
    "/minute",
    response_model=MinuteListResponse,
    operation_id="list_minute_bars",
    summary="Legacy one-minute bars (history only)",
    description=(
        "Historical TW one-minute bars from a retired provider; this dataset no longer "
        "updates. For current intraday data use `list_intraday_bars`. One instrument per "
        "call, newest first. `start_date` and `end_date` go together and may span at most "
        "five years; without them the month before the latest stored date is returned (see "
        "`range`). Volume is in shares."
    ),
    responses=_errors(
        {
            404: INSTRUMENT_NOT_FOUND,
            422: f"{DATASET_NOT_AVAILABLE} {INVALID_DATE_RANGE} {INVALID_CURSOR}",
            503: CONFIGURATION_INVALID,
        },
        paginated=True,
    ),
)
async def get_minute(
    instrument_id: InstrumentIdQuery,
    start_date: StartDateQuery = None,
    end_date: EndDateQuery = None,
    cursor: CursorQuery = None,
    page_size: Annotated[int, _page_size(100, 1000)] = 100,
    _: str | None = Depends(verify_paginated_serve_api_key(100)),
    db: AsyncSession = Depends(get_db),
) -> MinuteListResponse:
    return await serve_queries.list_minute(
        db,
        instrument_id=instrument_id,
        start_date=start_date,
        end_date=end_date,
        cursor=cursor,
        page_size=page_size,
    )


@router.get(
    "/intraday/bars",
    response_model=IntradayBarListResponse,
    operation_id="list_intraday_bars",
    summary="Intraday bars (1m to 60m)",
    description=(
        "Intraday bars of one TW equity or ETF, oldest first. `interval` aggregates the "
        "one-minute bars into 5m, 15m, 30m or 60m bars labelled by their end time; minutes "
        "without trades have no bar. `start_date` and `end_date` go together: at most 31 "
        "calendar days for `1m`, 366 for the others. Without them the latest stored trade date "
        "is returned (see `range`). Volume is in lots of 1,000 shares, not shares; see `units`. "
        "Always needs an API key; follow `pagination.next_cursor` until it is null."
    ),
    responses=_errors(
        {
            404: INSTRUMENT_NOT_FOUND,
            422: f"{DATASET_NOT_AVAILABLE} {INVALID_DATE_RANGE} {INVALID_CURSOR}",
            503: INTRADAY_UNAVAILABLE,
        },
        paginated=True,
        key_required=True,
    ),
)
async def get_intraday_bars(
    instrument_id: InstrumentIdQuery,
    interval: Annotated[IntradayInterval, Query(description="Bar interval.")] = "1m",
    start_date: StartDateQuery = None,
    end_date: EndDateQuery = None,
    cursor: CursorQuery = None,
    page_size: Annotated[int, _page_size(1000, 1000)] = 1000,
    _: str = Depends(require_intraday_serve_api_key(1000, cost=lambda page_size: 1)),
    db: AsyncSession = Depends(get_db),
) -> IntradayBarListResponse:
    return await serve_intraday.list_intraday_bars(
        db,
        instrument_id=instrument_id,
        interval=interval,
        start_date=start_date,
        end_date=end_date,
        cursor=cursor,
        page_size=page_size,
    )


@router.get(
    "/intraday/ticks",
    response_model=IntradayTickListResponse,
    operation_id="list_intraday_ticks",
    summary="Trade ticks of one day",
    description=(
        "Every trade of one TW equity or ETF on one trade date, in trade order. Without "
        "`trade_date` the latest stored date is used (`anchor`). `start_time` and `end_time` "
        "narrow the day by exchange-local time. Volume is in lots of 1,000 shares. Always "
        "needs an API key; each started 1,000 rows of `page_size` counts as one request "
        "against the rate limit."
    ),
    responses=_errors(
        {
            404: INSTRUMENT_NOT_FOUND,
            422: f"{DATASET_NOT_AVAILABLE} {INVALID_CURSOR}",
            503: INTRADAY_UNAVAILABLE,
        },
        paginated=True,
        key_required=True,
    ),
)
async def get_intraday_ticks(
    instrument_id: InstrumentIdQuery,
    trade_date: Annotated[
        date | None,
        Query(description="Exchange-local trade date (YYYY-MM-DD); default: latest stored."),
    ] = None,
    start_time: Annotated[
        time | None,
        Query(description="Earliest exchange-local time to include, `HH:MM[:SS]`, no timezone."),
    ] = None,
    end_time: Annotated[
        time | None,
        Query(description="Latest exchange-local time to include, `HH:MM[:SS]`, no timezone."),
    ] = None,
    cursor: CursorQuery = None,
    page_size: Annotated[int, _page_size(1000, 5000)] = 1000,
    # A tick page counts one unit per started thousand rows.
    _: str = Depends(
        require_intraday_serve_api_key(1000, cost=lambda page_size: -(-page_size // 1000))
    ),
    db: AsyncSession = Depends(get_db),
) -> IntradayTickListResponse:
    return await serve_intraday.list_intraday_ticks(
        db,
        instrument_id=instrument_id,
        trade_date=trade_date,
        start_time=start_time,
        end_time=end_time,
        cursor=cursor,
        page_size=page_size,
    )


@router.get(
    "/intraday/downloads",
    response_model=IntradayDownloadManifestResponse,
    operation_id="get_intraday_download_manifest",
    summary="Parquet download links for one instrument-month",
    description=(
        "For bulk research: presigned URLs of the Parquet objects holding one instrument's "
        "intraday bars or ticks for one month, valid until `expires_at`. Read only the trade "
        "dates listed under each object. Needs a key with the `intraday_download` scope; each "
        "object counts against the key's daily quota. Not useful inside a conversation: use "
        "`list_intraday_bars` or `list_intraday_ticks` instead."
    ),
    responses=_errors(
        {
            404: INSTRUMENT_NOT_FOUND,
            422: DATASET_NOT_AVAILABLE,
            429: (
                "`DOWNLOAD_QUOTA_EXCEEDED`: today's object quota is used up; `Retry-After` is "
                "the seconds until the next UTC day."
            ),
            503: INTRADAY_UNAVAILABLE,
        },
        key_required=True,
    ),
)
async def get_intraday_download_manifest(
    instrument_id: InstrumentIdQuery,
    month: Annotated[
        str,
        Query(
            min_length=7,
            max_length=7,
            description="Month to download, `YYYY-MM`.",
            examples=["2026-11"],
        ),
    ],
    kind: Annotated[
        Literal["bars", "ticks"], Query(description="`bars` for 1m bars, `ticks` for trades.")
    ] = "bars",
    key_id: str = Depends(
        require_intraday_serve_api_key(None, cost=lambda page_size: 1, scope=DOWNLOAD_SCOPE)
    ),
    db: AsyncSession = Depends(get_db),
) -> IntradayDownloadManifestResponse:
    return await serve_downloads.intraday_download_manifest(
        db, key_id=UUID(key_id), instrument_id=instrument_id, kind=kind, month=month
    )


@router.get(
    "/futures/eod",
    response_model=FuturesEODListResponse,
    operation_id="list_futures_eod",
    summary="TAIFEX futures daily quotes per contract",
    description=(
        "Daily quotes of individual TAIFEX futures contracts (each expiry separately; no "
        "continuous series), with `regular` and `after_hours` sessions as separate rows. "
        "Rows are ordered by trade date (`order`). Missing prices, settlement or open interest are "
        "null; a 0 volume means no trades. Follow `pagination.next_cursor` until it is null."
    ),
    responses=_errors(
        {
            422: (
                "`INVALID_PRODUCT`, `INVALID_SESSION` or `INVALID_CONTRACT`: unsupported value. "
                f"{DATASET_NOT_AVAILABLE} {INVALID_DATE_RANGE} {INVALID_CURSOR}"
            ),
            503: CONFIGURATION_INVALID,
        },
        paginated=True,
    ),
)
async def get_futures_eod(
    product_code: Annotated[
        str | None,
        Query(
            description="TAIFEX product, case-insensitive: `TX`, `MTX`, `TMF`, `TE` or `TF`.",
            examples=["TX"],
        ),
    ] = None,
    contract_code: Annotated[
        str | None,
        Query(description="One contract, `<product>:<contract_month>`.", examples=["TX:202612"]),
    ] = None,
    start_date: StartDateQuery = None,
    end_date: EndDateQuery = None,
    session: Annotated[
        str | None, Query(description="Only `regular` or only `after_hours` rows.")
    ] = None,
    order: Annotated[SortDirection, Query(description=ORDER_DESCRIPTION)] = "desc",
    cursor: CursorQuery = None,
    page_size: Annotated[int, _page_size(100, 1000)] = 100,
    _: str | None = Depends(verify_paginated_serve_api_key(100)),
    db: AsyncSession = Depends(get_db),
) -> FuturesEODListResponse:
    return await serve_queries.list_futures_eod(
        db,
        product_code=product_code,
        contract_code=contract_code,
        start_date=start_date,
        end_date=end_date,
        session=session,
        order=order,
        cursor=cursor,
        page_size=page_size,
    )


@router.get(
    "/futures/contracts",
    response_model=FuturesContractListResponse,
    operation_id="list_futures_contracts",
    summary="TAIFEX futures contracts and their quoted dates",
    description=(
        "Lists TAIFEX expiry contracts with the first and latest trade date FinDB holds quotes "
        "for, most recently quoted first. Use `active_on` to find the contracts that traded on "
        "a date (e.g. today's near-month contract), then pass `contract_code` to "
        "`list_futures_eod`. Expiry dates are not recorded yet."
    ),
    responses=_errors(
        {
            422: (
                "`INVALID_PRODUCT`: unsupported product code. "
                f"{DATASET_NOT_AVAILABLE} {INVALID_CURSOR}"
            ),
            503: CONFIGURATION_INVALID,
        },
        paginated=True,
    ),
)
async def get_futures_contracts(
    product_code: Annotated[
        str | None,
        Query(
            description="TAIFEX product, case-insensitive: `TX`, `MTX`, `TMF`, `TE` or `TF`.",
            examples=["TX"],
        ),
    ] = None,
    active_on: Annotated[
        date | None,
        Query(
            description=(
                "Only contracts quoted on or around this TAIFEX trade date: first quoted on or "
                "before it and last quoted on or after it."
            ),
            examples=["2026-11-20"],
        ),
    ] = None,
    cursor: CursorQuery = None,
    page_size: Annotated[int, _page_size(100, 1000)] = 100,
    _: str | None = Depends(verify_paginated_serve_api_key(100)),
    db: AsyncSession = Depends(get_db),
) -> FuturesContractListResponse:
    return await serve_queries.list_futures_contracts(
        db, product_code=product_code, active_on=active_on, cursor=cursor, page_size=page_size
    )


@router.get(
    "/exchange-quotes",
    response_model=ExchangeQuoteListResponse,
    operation_id="list_exchange_quotes",
    summary="Exchange official daily quotation report rows (HKEX)",
    description=(
        "Rows of the exchange's official daily quotation report for up to 50 instruments: "
        "previous close, official close, high, low, closing bid and ask, board and whether "
        "the security traded. Currently HK equities from HKEX. `list_eod` already carries the "
        "official close, high, low and volume for these instruments; use this for the "
        "previous close, bid/ask or trading status. Suspended securities have no row."
    ),
    responses=_errors(
        {
            404: f"{INSTRUMENT_NOT_FOUND} `details.inputs` lists the unknown values.",
            422: (
                f"{SELECTOR_ERRORS} {DATASET_NOT_AVAILABLE} {INVALID_DATE_RANGE} {INVALID_CURSOR}"
            ),
            503: CONFIGURATION_INVALID,
        },
        paginated=True,
    ),
)
async def get_exchange_quotes(
    instruments: InstrumentsQuery,
    start_date: StartDateQuery = None,
    end_date: EndDateQuery = None,
    order: Annotated[SortDirection, Query(description=ORDER_DESCRIPTION)] = "desc",
    cursor: CursorQuery = None,
    page_size: Annotated[int, _page_size(100, 1000)] = 100,
    _: str | None = Depends(verify_paginated_serve_api_key(100)),
    db: AsyncSession = Depends(get_db),
) -> ExchangeQuoteListResponse:
    return await serve_queries.list_exchange_quotes(
        db,
        instruments_raw=instruments,
        start_date=start_date,
        end_date=end_date,
        order=order,
        cursor=cursor,
        page_size=page_size,
    )


@router.get(
    "/calendar/years/{market}/{year}",
    response_model=PublishedCalendarYearEnvelope,
    operation_id="get_calendar_year",
    summary="Trading calendar of one whole year",
    description=(
        "Every date of one calendar year for one market, with open/closed status and session "
        "times. Always needs an API key. For a date range use `list_calendar_days`."
    ),
    responses=_errors({422: "Also for an unsupported market or year."}, key_required=True),
)
async def get_published_calendar_year(
    market: Annotated[str, Path(description=f"Calendar market: {CALENDAR_MARKETS}.")],
    year: Annotated[int, Path(description="Calendar year, e.g. 2026.")],
    _: str = Depends(require_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> PublishedCalendarYearEnvelope:
    try:
        year_data = await published_calendar_year(db, market, year)
    except ValueError as exc:
        raise serve_error(422, "INVALID_PARAMETER", str(exc)) from exc
    return PublishedCalendarYearEnvelope(
        data=PublishedCalendarYearResponse(
            **{
                **year_data,
                "days": [CalendarResponse(**day) for day in year_data["days"]],
            }
        )
    )


@router.get(
    "/calendar",
    response_model=CalendarListResponse,
    operation_id="list_calendar_days",
    summary="Trading calendar for a date range",
    description=(
        "Dates of one market's trading calendar, oldest first; filter with `is_open=true` "
        "to get trading days only. Without dates the current year is returned; a range may "
        "cover at most 3661 dates. Uses page-number pagination with totals."
    ),
    responses=_errors(
        {422: f"Also for an unsupported market. {INVALID_DATE_RANGE}"}, paginated=True
    ),
)
async def get_calendar(
    market: Annotated[
        str, Query(description=f"Calendar market: {CALENDAR_MARKETS}.", examples=["TW"])
    ],
    start_date: Annotated[
        date | None, Query(description="First date to include (YYYY-MM-DD).")
    ] = None,
    end_date: Annotated[
        date | None, Query(description="Last date to include (YYYY-MM-DD).")
    ] = None,
    is_open: Annotated[
        bool | None,
        Query(description="true: trading days only; false: closed days only."),
    ] = None,
    page: Annotated[int, Query(ge=1, description="Page number, starting at 1.")] = 1,
    page_size: Annotated[int, _page_size(100, 1000)] = 100,
    _: str | None = Depends(verify_paginated_serve_api_key(100)),
    db: AsyncSession = Depends(get_db),
) -> CalendarListResponse:
    return await serve_queries.list_calendar(
        db,
        market=market,
        start_date=start_date,
        end_date=end_date,
        is_open=is_open,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/market-freshness",
    response_model=MarketFreshnessSummaryListResponse,
    operation_id="list_market_freshness",
    summary="How up to date each market's data is",
    description=(
        "For each market: the trade date that should be available by now, the latest date "
        "that is fully available, and whether today's update is fresh, partial or late. Use "
        "it to tell a user whether the latest close is already in FinDB."
    ),
    responses=_errors(),
)
async def get_market_freshness(
    market: Annotated[
        str | None,
        Query(min_length=1, max_length=10, description="Only this market, e.g. `TW`."),
    ] = None,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> MarketFreshnessSummaryListResponse:
    rows = await list_market_freshness(db, market=market)
    return MarketFreshnessSummaryListResponse(
        data=[
            MarketFreshnessSummaryResponse(
                market=row.market,
                slot_id=cast(CanonicalSlotId, row.slot_id),
                scheduled_local_time=row.scheduled_local_time,
                timezone=row.timezone,
                status=row.status,
                expected_data_date=row.expected_data_date,
                coverage_data_date=row.coverage_data_date,
                last_successful_update_at=row.last_successful_update_at,
                last_complete_at=row.last_complete_at,
                next_scheduled_at=row.next_scheduled_at,
                feed_count=row.feed_count,
                fresh_feed_count=row.fresh_feed_count,
                late_feed_count=row.late_feed_count,
            )
            for row in rows
        ]
    )
