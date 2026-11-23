"""Typed response contracts for the canonical read-only Serve API.

Every field carries an English description: clients generate LLM tool
definitions from the published OpenAPI document, so the schema text is the
documentation an agent reads.
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PaginationInfo
from app.services.slot_identity import CanonicalSlotId

SortDirection = Literal["asc", "desc"]
InstrumentSort = Literal[
    "relevance",
    "market",
    "symbol",
    "name",
    "asset_class",
    "currency",
    "status",
    "eod_first_date",
    "eod_latest_date",
    "eod_latest_close",
    "minute_first_bar_at",
    "minute_latest_bar_at",
    "minute_latest_close",
    "futures_first_date",
    "futures_latest_date",
]

VolumeUnit = Literal["share", "lot", "contract", "provider_defined"]
VOLUME_UNIT = (
    "`share`: shares; `lot`: board lots of 1,000 shares; `contract`: futures contracts; "
    "`provider_defined`: the provider's own figure (indices), not comparable across instruments."
)

INSTRUMENT_ID = "FinDB instrument UUID; use it with the data endpoints."
MARKET = "Market code, e.g. `TW`, `US`, `HK`."
SYMBOL = "Exchange symbol in FinDB form, e.g. `2330` (TW), `AAPL` (US), `00700` (HK)."
TRADE_DATE = "Exchange-local trading date (YYYY-MM-DD)."
SOURCE = "Provider the row's values came from, e.g. `finlab`, `twelve_data`, `factset`."
SOURCE_FETCHED_AT = "UTC time the provider data was fetched."
ASOF_TS = "UTC time the provider says the values are as of."
CREATED_AT = "UTC time FinDB first stored this row."
UPDATED_AT = "UTC time FinDB last changed this row."
UNADJUSTED_PRICE = "price as traded, not adjusted for splits or dividends"


class ServeErrorDetail(BaseModel):
    code: str = Field(
        description=(
            "Stable machine-readable error code; branch on this, not on `message`. "
            "Examples: `INVALID_PARAMETER`, `INSTRUMENT_NOT_FOUND`, `DATASET_NOT_AVAILABLE`, "
            "`API_KEY_REQUIRED`, `API_KEY_INVALID`, `SCOPE_REQUIRED`, "
            "`PAGE_SIZE_EXCEEDS_KEY_LIMIT`, `RATE_LIMITED`."
        )
    )
    message: str = Field(description="Human-readable explanation of the error.")
    details: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Extra context when available, e.g. `errors` (one entry per invalid parameter "
            "with `location`, `param` and `reason`), `limit`, `scope`, or "
            "`retry_after_seconds`."
        ),
    )


class ServeErrorResponse(BaseModel):
    """Body of every Serve error response."""

    detail: ServeErrorDetail = Field(description="The error.")


class EODCoverageResponse(BaseModel):
    first_date: date = Field(description="First trade date stored.")
    latest_date: date = Field(description="Latest trade date stored.")
    latest_close: Decimal | None = Field(
        default=None, description="Close on `latest_date`; null for futures products."
    )


class MinuteCoverageResponse(BaseModel):
    first_bar_at: datetime = Field(description="UTC start of the first stored legacy minute bar.")
    latest_bar_at: datetime = Field(description="UTC start of the latest stored legacy minute bar.")
    latest_close: Decimal = Field(description="Close of the latest legacy minute bar.")


class IntradayCoverageResponse(BaseModel):
    """Exchange-local trade dates of the first and latest stored day."""

    first_date: date = Field(description="First exchange-local trade date stored.")
    latest_date: date = Field(description="Latest exchange-local trade date stored.")


class InstrumentCoverageResponse(BaseModel):
    """Which data each endpoint holds for the instrument; null means none."""

    eod: EODCoverageResponse | None = Field(
        default=None, description="Daily bars served by `list_eod`."
    )
    minute: MinuteCoverageResponse | None = Field(
        default=None,
        description="Legacy one-minute bars served by `list_minute_bars` (history only).",
    )
    futures: EODCoverageResponse | None = Field(
        default=None, description="Futures contract quotes served by `list_futures_eod`."
    )
    intraday_bars: IntradayCoverageResponse | None = Field(
        default=None, description="Intraday bars served by `list_intraday_bars`."
    )
    intraday_ticks: IntradayCoverageResponse | None = Field(
        default=None, description="Trade ticks served by `list_intraday_ticks`."
    )


class InstrumentResponse(BaseModel):
    instrument_id: UUID = Field(description=INSTRUMENT_ID)
    asset_class: str = Field(description="Asset class, e.g. `equity`, `etf`, `index`, `future`.")
    market: str = Field(description=MARKET)
    symbol: str = Field(description=SYMBOL + " For TW futures this is the product code, e.g. `TX`.")
    name: str | None = Field(default=None, description="Instrument name as the provider gives it.")
    currency: str | None = Field(default=None, description="ISO 4217 quote currency.")
    timezone: str | None = Field(default=None, description="IANA timezone; often null.")
    status: str = Field(description="Listing status, e.g. `active`.")
    listed_date: date | None = Field(default=None, description="Listing date; often null.")
    delisted_date: date | None = Field(default=None, description="Delisting date; often null.")
    coverage: InstrumentCoverageResponse = Field(
        description="Stored date range per data type; use it to pick an endpoint and dates."
    )


class InstrumentFacetsResponse(BaseModel):
    markets: list[str] = Field(description="Markets present in the active catalog.")
    asset_classes: list[str] = Field(description="Asset classes present in the active catalog.")
    statuses: list[str] = Field(description="Instrument statuses present in the active catalog.")


class InstrumentListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    data: list[InstrumentResponse] = Field(description="Instruments on this page.")
    pagination: PaginationInfo = Field(description="Page-number pagination with totals.")
    facets: InstrumentFacetsResponse = Field(
        description="Filter values available across the whole catalog."
    )


class InstrumentResolution(BaseModel):
    input: str = Field(description="The value as the caller sent it.")
    market: str = Field(description="Market the value was looked up in.")
    symbol: str = Field(
        description="Symbol as FinDB stores it, e.g. `00700` for an input of `HK:700`."
    )
    status: Literal["resolved", "ambiguous", "not_found", "not_available"] = Field(
        description=(
            "`resolved`: exactly one match. `ambiguous`: several asset classes share the "
            "symbol; pick one `instrument_id` from `matches`. `not_found`: FinDB has no such "
            "instrument. `not_available`: the instrument exists but FinDB serves no data for it."
        )
    )
    matches: list[InstrumentResponse] = Field(
        description="Matching instruments with their coverage; empty unless resolved or ambiguous."
    )


class InstrumentResolveResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    data: list[InstrumentResolution] = Field(
        description="One entry per distinct input, in request order."
    )


class InstrumentDetailResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    data: InstrumentResponse = Field(description="The instrument.")


class DatasetCoverageResponse(BaseModel):
    coverage_start_date: date | None = Field(
        default=None, description="Earliest trade date stored; null when empty."
    )
    coverage_end_date: date | None = Field(
        default=None, description="Latest trade date stored; null when empty."
    )
    instrument_count: int = Field(description="Number of instruments with stored data.")


class DatasetResponse(BaseModel):
    dataset_key: str = Field(description="Stable dataset identifier, e.g. `tw_equity_eod`.")
    display_name: str = Field(description="Short human-readable name.")
    market: str = Field(description=MARKET)
    asset_class: str = Field(description="Asset class, e.g. `equity`, `etf`, `index`, `future`.")
    frequency: Literal["daily", "minute", "tick"] = Field(description="Sampling frequency.")
    data_kind: Literal[
        "eod", "minute", "futures_eod", "intraday_bars", "intraday_ticks", "exchange_quotes"
    ] = Field(
        description=(
            "Kind of data; `operation_id` names the endpoint that serves it. "
            "`exchange_quotes` is the exchange's official daily report (HKEX), whose values "
            "also correct the `eod` rows."
        )
    )
    interval: Literal["1d", "1m", "tick"] = Field(description="Native bar interval.")
    operation_id: str = Field(
        description="Operation that serves the data, e.g. `list_eod` or `list_intraday_bars`."
    )
    volume_unit: VolumeUnit = Field(description=f"Unit of `volume` in this dataset. {VOLUME_UNIT}")
    availability: Literal["available", "configured_empty"] = Field(
        description="`configured_empty` means the dataset is active but holds no rows yet."
    )
    coverage: DatasetCoverageResponse = Field(description="Stored date range and size.")


class DatasetListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    data: list[DatasetResponse] = Field(description="Every active dataset.")


class CursorPaginationInfo(BaseModel):
    page_size: int = Field(description="Maximum rows requested for this page.")
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque cursor for the next page; pass it back unchanged with the same other "
            "parameters. Null when there are no more rows. A page may hold fewer rows than "
            "`page_size` and still have a next page."
        ),
    )


class EODResponse(BaseModel):
    instrument_id: UUID = Field(description=INSTRUMENT_ID)
    symbol: str = Field(description=SYMBOL)
    name: str | None = Field(default=None, description="Instrument name.")
    market: str = Field(description=MARKET)
    currency: str | None = Field(
        default=None, description="ISO 4217 currency of prices and turnover."
    )
    trade_date: date = Field(description=TRADE_DATE)
    open: Decimal | None = Field(default=None, description=f"Opening {UNADJUSTED_PRICE}.")
    high: Decimal | None = Field(default=None, description=f"Highest {UNADJUSTED_PRICE}.")
    low: Decimal | None = Field(default=None, description=f"Lowest {UNADJUSTED_PRICE}.")
    close: Decimal | None = Field(
        default=None,
        description=(
            f"Closing {UNADJUSTED_PRICE}. HK closes are the exchange's official close and can "
            "lie outside high/low."
        ),
    )
    volume: int | None = Field(
        default=None,
        description=(
            "Shares traded for equities and ETFs. For indices it is the provider's figure "
            "and not comparable across indices."
        ),
    )
    total_ticks: int | None = Field(
        default=None, description="Number of trades, when the provider reports it."
    )
    turnover: Decimal | None = Field(
        default=None, description="Traded value in `currency`, when the provider reports it."
    )
    source: str | None = Field(default=None, description=SOURCE)
    field_sources: dict[str, str] | None = Field(
        default=None,
        description=(
            "Fields whose value did not come from `source`, mapped to where it came from: "
            '`{"close": "hkex"}` after the exchange\'s official quote replaced it, `"manual"` '
            "after an operator correction. Null when every value is `source`'s own."
        ),
    )
    source_fetched_at: datetime | None = Field(default=None, description=SOURCE_FETCHED_AT)
    asof_ts: datetime = Field(description=ASOF_TS)
    created_at: datetime = Field(description=CREATED_AT)
    updated_at: datetime = Field(description=UPDATED_AT)

    model_config = ConfigDict(from_attributes=True)


class EODUnitsResponse(BaseModel):
    """How to read daily rows."""

    price_adjustment: Literal["none"] = Field(
        default="none", description="Prices are as traded, not adjusted for splits or dividends."
    )
    trade_date: Literal["exchange_local"] = Field(
        default="exchange_local", description="`trade_date` is the exchange-local date."
    )
    instants: Literal["UTC"] = Field(default="UTC", description="Timestamps are UTC.")
    volume_by_asset_class: dict[str, VolumeUnit] = Field(
        description=f"Unit of `volume` per asset class, e.g. `equity: share`. {VOLUME_UNIT}"
    )


class EODListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    units: EODUnitsResponse = Field(description="How to read prices, dates and volumes.")
    data: list[EODResponse] = Field(
        description="Daily bars ordered by trade date (see `order`), then by instrument_id."
    )
    pagination: CursorPaginationInfo = Field(description="Cursor pagination.")


class InstrumentSummaryResponse(BaseModel):
    instrument_id: UUID = Field(description=INSTRUMENT_ID)
    symbol: str = Field(description=SYMBOL)
    name: str | None = Field(default=None, description="Instrument name.")
    market: str = Field(description=MARKET)
    asset_class: str = Field(description="Asset class, e.g. `equity`, `etf`, `index`.")
    currency: str | None = Field(default=None, description="ISO 4217 quote currency.")


class EODLatestQuote(BaseModel):
    input: str = Field(description="The value as the caller sent it.")
    instrument: InstrumentSummaryResponse = Field(description="The instrument.")
    trade_date: date = Field(description="Exchange-local date of the latest stored bar.")
    open: Decimal | None = Field(default=None, description=f"Opening {UNADJUSTED_PRICE}.")
    high: Decimal | None = Field(default=None, description=f"Highest {UNADJUSTED_PRICE}.")
    low: Decimal | None = Field(default=None, description=f"Lowest {UNADJUSTED_PRICE}.")
    close: Decimal | None = Field(default=None, description=f"Closing {UNADJUSTED_PRICE}.")
    volume: int | None = Field(default=None, description="Volume; see `units`.")
    turnover: Decimal | None = Field(default=None, description="Traded value in `currency`.")
    previous_trade_date: date | None = Field(
        default=None, description="Date of the stored bar before `trade_date`; null if none."
    )
    previous_close: Decimal | None = Field(
        default=None, description="Close on `previous_trade_date`."
    )
    change: Decimal | None = Field(
        default=None, description="`close - previous_close`; null when either is missing."
    )
    change_percent: Decimal | None = Field(
        default=None,
        description="`change / previous_close * 100`, 4 decimals; null when not computable.",
    )
    source: str | None = Field(default=None, description=SOURCE)
    field_sources: dict[str, str] | None = Field(
        default=None, description="Fields whose value did not come from `source`; see `list_eod`."
    )


class EODLatestMissing(BaseModel):
    input: str = Field(description="The value as the caller sent it.")
    reason: Literal["not_found", "ambiguous", "not_available", "no_data"] = Field(
        description=(
            "`not_found`: no such instrument. `ambiguous`: several asset classes share the "
            "symbol; resend one `instrument_id` from `matches`. `not_available`: FinDB serves "
            "no daily bars for it. `no_data`: no bar on or before `as_of_date`."
        )
    )
    matches: list[InstrumentSummaryResponse] = Field(
        description="Candidates when `ambiguous`; otherwise empty."
    )


class EODLatestResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    as_of_date: date | None = Field(
        default=None, description="Latest trade date considered; null means the latest stored."
    )
    units: EODUnitsResponse = Field(description="How to read prices, dates and volumes.")
    data: list[EODLatestQuote] = Field(description="One quote per found instrument, in order.")
    missing: list[EODLatestMissing] = Field(
        description="Inputs without a quote; they do not fail the request."
    )


class FuturesEODResponse(BaseModel):
    contract_id: UUID = Field(description="FinDB UUID of the expiry contract.")
    instrument_id: UUID = Field(description="FinDB UUID of the futures product.")
    product_code: str = Field(description="TAIFEX product: `TX`, `MTX`, `TMF`, `TE` or `TF`.")
    contract_code: str = Field(
        description="`<product_code>:<contract_month>`, e.g. `TX:202612` or `TX:202612W2`."
    )
    contract_month: str | None = Field(
        default=None,
        description=(
            "TAIFEX expiry identifier: `YYYYMM` for monthly, `YYYYMMW1`-`W5` for weekly and "
            "`YYYYMMF1`-`F5` for Friday-weekly contracts."
        ),
    )
    trade_date: date = Field(
        description=(
            "TAIFEX trade date. An `after_hours` session runs from the previous afternoon to "
            "05:00 Taipei and belongs to this date."
        )
    )
    session: Literal["regular", "after_hours"] = Field(description="Trading session.")
    open: Decimal | None = Field(default=None, description="Opening price; null if none.")
    high: Decimal | None = Field(default=None, description="Highest price; null if none.")
    low: Decimal | None = Field(default=None, description="Lowest price; null if none.")
    close: Decimal | None = Field(default=None, description="Last price; null if no trade.")
    volume: int | None = Field(default=None, description="Contracts traded; 0 means no trade.")
    settlement_price: Decimal | None = Field(
        default=None, description="Daily settlement price; null when not published."
    )
    open_interest: int | None = Field(
        default=None, description="Open contracts at session end; null when not published."
    )
    source: str | None = Field(default=None, description=SOURCE)
    source_fetched_at: datetime | None = Field(default=None, description=SOURCE_FETCHED_AT)
    asof_ts: datetime = Field(description=ASOF_TS)
    created_at: datetime = Field(description=CREATED_AT)
    updated_at: datetime = Field(description=UPDATED_AT)


class FuturesUnitsResponse(BaseModel):
    """How to read futures rows."""

    price_adjustment: Literal["none"] = Field(
        default="none", description="Prices are as traded; there is no continuous series."
    )
    trade_date: Literal["exchange_local"] = Field(
        default="exchange_local", description="`trade_date` is the TAIFEX trade date."
    )
    instants: Literal["UTC"] = Field(default="UTC", description="Timestamps are UTC.")
    volume: Literal["contract"] = Field(
        default="contract", description="`volume` counts contracts traded."
    )
    open_interest: Literal["contract"] = Field(
        default="contract", description="`open_interest` counts open contracts."
    )


class FuturesEODListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    units: FuturesUnitsResponse = Field(description="How to read prices, dates and volumes.")
    data: list[FuturesEODResponse] = Field(
        description="Contract quotes ordered by trade date (see `order`)."
    )
    pagination: CursorPaginationInfo = Field(description="Cursor pagination.")


class FuturesContractResponse(BaseModel):
    contract_id: UUID = Field(description="FinDB UUID of the expiry contract.")
    instrument_id: UUID = Field(description="FinDB UUID of the futures product.")
    product_code: str = Field(description="TAIFEX product, e.g. `TX`.")
    contract_code: str = Field(
        description="`<product_code>:<contract_month>`; pass it to `list_futures_eod`."
    )
    contract_month: str | None = Field(
        default=None, description="TAIFEX expiry identifier, e.g. `202612` or `202612W2`."
    )
    contract_type: Literal["monthly", "weekly", "friday_weekly"] | None = Field(
        default=None,
        description="From `contract_month`: plain `YYYYMM`, a `W` suffix or an `F` suffix.",
    )
    expiry_date: date | None = Field(
        default=None, description="Expiry date; not yet recorded by FinDB, so currently null."
    )
    first_trade_date: date = Field(description="First trade date FinDB holds a quote for.")
    last_trade_date: date = Field(
        description=(
            "Latest trade date FinDB holds a quote for; for a live contract this is the latest "
            "stored trading day."
        )
    )


class FuturesContractListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    data: list[FuturesContractResponse] = Field(
        description="Contracts, most recently quoted first, then by contract code."
    )
    pagination: CursorPaginationInfo = Field(description="Cursor pagination.")


class ExchangeQuoteResponse(BaseModel):
    instrument_id: UUID = Field(description=INSTRUMENT_ID)
    symbol: str = Field(description=SYMBOL)
    name: str | None = Field(default=None, description="Instrument name.")
    market: str = Field(description=MARKET)
    trade_date: date = Field(description=TRADE_DATE)
    board: str = Field(description="Exchange board, e.g. HKEX `main` or `gem`.")
    currency: str = Field(description="ISO 4217 currency of the trading counter.")
    trading_status: Literal["traded", "no_trade"] = Field(
        description=(
            "`traded` when volume is positive, `no_trade` when it is 0. Suspended securities "
            "are not in the report, so they have no row."
        )
    )
    previous_close: Decimal | None = Field(
        default=None, description="Previous close as the exchange reports it."
    )
    close: Decimal | None = Field(
        default=None,
        description=(
            "Official close. Without a closing auction it is the median of last-minute "
            "nominal prices, not necessarily a trade, and can lie outside high/low."
        ),
    )
    high: Decimal | None = Field(default=None, description="Highest price; excludes odd lots.")
    low: Decimal | None = Field(default=None, description="Lowest price; excludes odd lots.")
    bid: Decimal | None = Field(default=None, description="Best bid at the close.")
    ask: Decimal | None = Field(default=None, description="Best ask at the close.")
    volume: int = Field(description="Shares traded.")
    turnover: Decimal = Field(description="Traded value in `currency`.")
    source: str = Field(description="Exchange the report came from, e.g. `hkex`.")
    source_fetched_at: datetime = Field(description=SOURCE_FETCHED_AT)


class ExchangeQuoteListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    data: list[ExchangeQuoteResponse] = Field(
        description="Report rows ordered by trade date (see `order`), then by instrument."
    )
    pagination: CursorPaginationInfo = Field(description="Cursor pagination.")


class MinuteResponse(BaseModel):
    instrument_id: UUID = Field(description=INSTRUMENT_ID)
    symbol: str = Field(description=SYMBOL)
    name: str | None = Field(default=None, description="Instrument name.")
    market: str = Field(description=MARKET)
    trade_date: date = Field(description="Taiwan local trading date.")
    bar_start_time: datetime = Field(description="UTC start of the minute.")
    bar_end_time: datetime = Field(description="UTC end of the minute.")
    signal_time: datetime = Field(description="UTC time the bar is final; equals bar_end_time.")
    market_timezone: str = Field(description="Always `Asia/Taipei`.")
    open: Decimal = Field(description=f"Opening {UNADJUSTED_PRICE}.")
    high: Decimal = Field(description=f"Highest {UNADJUSTED_PRICE}.")
    low: Decimal = Field(description=f"Lowest {UNADJUSTED_PRICE}.")
    close: Decimal = Field(description=f"Closing {UNADJUSTED_PRICE}.")
    volume: int | None = Field(default=None, description="Shares traded in the minute.")
    turnover: Decimal | None = Field(default=None, description="Traded value in TWD.")
    trade_count: int | None = Field(default=None, description="Always null for this dataset.")
    price_adjustment: str = Field(description="Always `none`.")
    source: str = Field(description=SOURCE)
    source_fetched_at: datetime = Field(description=SOURCE_FETCHED_AT)
    asof_ts: datetime = Field(description=ASOF_TS)
    created_at: datetime = Field(description=CREATED_AT)
    updated_at: datetime = Field(description=UPDATED_AT)

    model_config = ConfigDict(from_attributes=True)


class ResolvedDateRangeResponse(BaseModel):
    start_date: date | None = Field(
        default=None, description="First trade date the query covers (inclusive)."
    )
    end_date: date | None = Field(
        default=None, description="Last trade date the query covers (inclusive)."
    )
    anchor: Literal["explicit", "latest_available"] = Field(
        description=(
            "`explicit` when the caller gave the dates; `latest_available` when FinDB chose "
            "them from the latest stored data."
        )
    )


class MinuteListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    data: list[MinuteResponse] = Field(description="Minute bars, newest first.")
    pagination: CursorPaginationInfo = Field(description="Cursor pagination.")
    range: ResolvedDateRangeResponse = Field(description="Dates the query actually covers.")


IntradayInterval = Literal["1m", "5m", "15m", "30m", "60m"]


class IntradayUnitsResponse(BaseModel):
    """How to read the rows; volumes are lots, unlike the share-based EOD."""

    volume: Literal["lot"] = Field(
        default="lot",
        description="Volumes are lots (board lots of 1,000 shares), not shares.",
    )
    price_adjustment: Literal["none"] = Field(
        default="none", description="Prices are as traded, not adjusted."
    )
    instants: Literal["UTC"] = Field(default="UTC", description="Timestamps are UTC.")
    trade_date: Literal["exchange_local"] = Field(
        default="exchange_local", description="`trade_date` is the exchange-local date."
    )
    market_timezone: str = Field(description="IANA timezone of the exchange.")


class IntradayBarResponse(BaseModel):
    trade_date: date = Field(description=TRADE_DATE)
    bar_start_time: datetime = Field(description="UTC start of the bar.")
    bar_end_time: datetime = Field(
        description="UTC end of the bar; aggregated bars are labelled by their end time."
    )
    session: str = Field(description="Trading session of the bar, e.g. `regular`.")
    open: Decimal = Field(description=f"Opening {UNADJUSTED_PRICE}.")
    high: Decimal = Field(description=f"Highest {UNADJUSTED_PRICE}.")
    low: Decimal = Field(description=f"Lowest {UNADJUSTED_PRICE}.")
    close: Decimal = Field(description=f"Closing {UNADJUSTED_PRICE}.")
    volume: int = Field(description="Lots traded in the bar.")
    trade_count: int | None = Field(
        default=None, description="Trades in the bar; null if any minute lacks it."
    )
    vwap: Decimal | None = Field(
        default=None, description="Volume-weighted average price; null if any minute lacks it."
    )
    source: str = Field(description=SOURCE)
    source_fetched_at: datetime = Field(description=SOURCE_FETCHED_AT)


class IntradayBarListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    instrument: InstrumentSummaryResponse = Field(description="The instrument queried.")
    dataset_key: str = Field(description="Dataset the rows come from.")
    interval: IntradayInterval = Field(description="Bar interval of the rows.")
    units: IntradayUnitsResponse = Field(description="How to read volumes, prices and times.")
    data: list[IntradayBarResponse] = Field(
        description="Bars, oldest first; minutes without trades have no bar."
    )
    pagination: CursorPaginationInfo = Field(description="Cursor pagination.")
    range: ResolvedDateRangeResponse = Field(description="Dates the query actually covers.")


class IntradayTickResponse(BaseModel):
    trade_date: date = Field(description=TRADE_DATE)
    trade_time: datetime = Field(description="UTC time of the trade.")
    sequence: int = Field(description="Order of the trade within the day.")
    session: str = Field(description="Trading session, e.g. `regular`.")
    price: Decimal = Field(description=f"Trade {UNADJUSTED_PRICE}.")
    volume: int = Field(description="Lots traded.")
    tick_type: int | None = Field(
        default=None, description="Provider's trade-side flag, when given."
    )
    bid_price: Decimal | None = Field(default=None, description="Best bid at the trade.")
    bid_volume: int | None = Field(default=None, description="Lots at the best bid.")
    ask_price: Decimal | None = Field(default=None, description="Best ask at the trade.")
    ask_volume: int | None = Field(default=None, description="Lots at the best ask.")
    source: str = Field(description=SOURCE)
    source_fetched_at: datetime = Field(description=SOURCE_FETCHED_AT)


class IntradayTickListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    instrument: InstrumentSummaryResponse = Field(description="The instrument queried.")
    dataset_key: str = Field(description="Dataset the rows come from.")
    units: IntradayUnitsResponse = Field(description="How to read volumes, prices and times.")
    trade_date: date | None = Field(
        default=None, description="Trade date queried; null when nothing is stored."
    )
    anchor: Literal["explicit", "latest_available"] = Field(
        description="`latest_available` when FinDB picked the latest stored trade date."
    )
    data: list[IntradayTickResponse] = Field(description="Ticks in trade order.")
    pagination: CursorPaginationInfo = Field(description="Cursor pagination.")


class IntradayDownloadColumn(BaseModel):
    name: str = Field(description="Parquet column name.")
    type: str = Field(description="Parquet column type.")
    nullable: bool = Field(description="Whether the column can be null.")
    description: str = Field(description="What the column holds.")


class IntradayDownloadLayout(BaseModel):
    """The columns every object in the manifest holds, as the contract publishes them."""

    schema_id: Literal["intraday_partition"] = Field(
        default="intraday_partition", description="Contract the objects follow."
    )
    schema_version: int = Field(description="Contract version.")
    record_kind: Literal["bar_1m", "tick"] = Field(description="Row type in the objects.")
    format: Literal["parquet"] = Field(default="parquet", description="File format.")
    columns: list[IntradayDownloadColumn] = Field(description="Columns of every object.")


class IntradayDownloadDay(BaseModel):
    trade_date: date = Field(description="Trade date to take from the object.")
    row_count: int = Field(description="Rows that date must have.")


class IntradayDownloadObject(BaseModel):
    """One object to fetch, and the only trade dates to take from it.

    A month object can still hold rows of a day republished after the merge;
    those rows are superseded and that day is listed under its day object.
    """

    url: str = Field(description="Presigned HTTPS URL; valid until `expires_at`.")
    object_layout: Literal["day", "month"] = Field(
        description="Whether the object holds one day or a merged month."
    )
    object_etag: str = Field(
        description="Expected ETag; a different ETag means the object was replaced."
    )
    trade_dates: list[IntradayDownloadDay] = Field(
        description="The only trade dates to read from this object."
    )


class IntradayDownloadQuota(BaseModel):
    usage_date: date = Field(description="UTC date the quota applies to.")
    daily_limit: int = Field(description="Objects the key may receive per UTC day.")
    used: int = Field(description="Objects issued today, this manifest included.")
    remaining: int = Field(description="Objects left today.")


class IntradayDownloadManifestResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    instrument: InstrumentSummaryResponse = Field(description="The instrument queried.")
    dataset_key: str = Field(description="Dataset the objects belong to.")
    month: str = Field(description="Month covered, `YYYY-MM`.")
    units: IntradayUnitsResponse = Field(description="How to read volumes, prices and times.")
    layout: IntradayDownloadLayout = Field(description="Columns of the objects.")
    expires_at: datetime = Field(description="UTC time the URLs stop working.")
    objects: list[IntradayDownloadObject] = Field(description="Objects to download.")
    quota: IntradayDownloadQuota = Field(description="The key's daily download quota.")


class CalendarResponse(BaseModel):
    market: str = Field(description="Calendar market code, e.g. `TW`, `US`, `HK`, `TAIFEX`.")
    trade_date: date = Field(description="Exchange-local date.")
    is_open: bool = Field(description="Whether the market trades on this date.")
    session_open: str | None = Field(
        default=None, description="Exchange-local opening time `HH:MM:SS`; null when closed."
    )
    session_close: str | None = Field(
        default=None, description="Exchange-local closing time `HH:MM:SS`; null when closed."
    )
    holiday_name: str | None = Field(default=None, description="Holiday name when closed.")
    day_status: Literal["open", "closed", "settlement_only"] = Field(
        default="open",
        description="`settlement_only` means no trading but settlement still happens.",
    )
    description: str | None = Field(default=None, description="Operator note for the date.")
    calendar_source: Literal["published", "weekday_default"] = Field(
        default="published",
        description=(
            "`published` when an official calendar covers the date; `weekday_default` when "
            "FinDB assumed weekdays open and weekends closed."
        ),
    )
    calendar_revision: int | None = Field(
        default=None, description="Revision of the published calendar; null when defaulted."
    )

    model_config = ConfigDict(from_attributes=True)


class CalendarListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    data: list[CalendarResponse] = Field(description="Calendar dates, oldest first.")
    pagination: PaginationInfo = Field(description="Page-number pagination with totals.")


class PublishedCalendarYearResponse(BaseModel):
    market: str = Field(description="Calendar market code.")
    year: int = Field(description="Calendar year.")
    revision: int | None = Field(description="Published revision; null when fully defaulted.")
    status: Literal["published", "defaulted", "mixed"] = Field(
        description="Whether the days come from a published calendar, weekday defaults, or both."
    )
    coverage_complete: Literal[True] = Field(description="Always true: every date is listed.")
    timezone: str = Field(description="IANA timezone of the market.")
    expected_days: int = Field(description="Days in the year.")
    actual_days: int = Field(description="Days listed.")
    days: list[CalendarResponse] = Field(description="Every date of the year, oldest first.")


class PublishedCalendarYearEnvelope(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    data: PublishedCalendarYearResponse = Field(description="The calendar year.")


class MarketFreshnessSummaryResponse(BaseModel):
    market: str = Field(description=MARKET)
    slot_id: CanonicalSlotId = Field(description="Daily update window the market belongs to.")
    scheduled_local_time: time = Field(description="Local time the update is scheduled.")
    timezone: str = Field(description="IANA timezone of `scheduled_local_time`.")
    status: Literal["not_due", "fresh", "partial", "late", "failed", "never_received"] = Field(
        description=(
            "`fresh`: the expected date is complete; `partial`: only part arrived; `late`: "
            "past due; `failed`: the update failed; `not_due`: nothing is expected yet; "
            "`never_received`: no data has ever arrived."
        )
    )
    expected_data_date: date | None = Field(
        default=None, description="Trade date that should be available by now."
    )
    coverage_data_date: date | None = Field(
        default=None, description="Latest trade date fully available."
    )
    last_successful_update_at: datetime | None = Field(
        default=None, description="UTC time of the last successful update."
    )
    last_complete_at: datetime | None = Field(
        default=None, description="UTC time the market last became complete."
    )
    next_scheduled_at: datetime = Field(description="UTC time of the next scheduled update.")
    feed_count: int = Field(description="Feeds that update this market.")
    fresh_feed_count: int = Field(description="Feeds that are fresh.")
    late_feed_count: int = Field(description="Feeds that are late, partial or never received.")


class MarketFreshnessSummaryListResponse(BaseModel):
    success: Literal[True] = Field(default=True, description="Always true on success.")
    data: list[MarketFreshnessSummaryResponse] = Field(description="One entry per market.")
