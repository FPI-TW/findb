export type DatasetKey = "instruments"
export type SortDirection = "asc" | "desc"
export type LookupSortKey =
  | "market"
  | "symbol"
  | "name"
  | "asset_class"
  | "currency"
  | "status"
  | "eod_first_date"
  | "eod_latest_date"
  | "eod_latest_close"
  | "minute_first_bar_at"
  | "minute_latest_bar_at"
  | "minute_latest_close"

export interface EodCoverage {
  first_date: string
  latest_date: string
  latest_close: string | number | null
}

export interface MinuteCoverage {
  first_bar_at: string
  latest_bar_at: string
  latest_close: string | number
}

export interface Instrument {
  instrument_id: string
  market: string
  asset_class: string
  symbol: string
  name: string | null
  currency: string | null
  timezone: string | null
  status: string
  listed_date: string | null
  delisted_date: string | null
  coverage: {
    eod: EodCoverage | null
    minute: MinuteCoverage | null
  }
}

export type LookupItem = Instrument

export interface LookupPagination {
  page: number
  page_size: number
  total_records: number
  total_pages: number
}

export interface InstrumentFacets {
  markets: string[]
  asset_classes: string[]
  statuses: string[]
}

export interface InstrumentLookupResponse {
  success: boolean
  data: Instrument[]
  pagination: LookupPagination
  facets: InstrumentFacets
}

export type LookupResponse = InstrumentLookupResponse

export interface LookupSearch {
  ds: DatasetKey
  q: string
  m: string
  ac: string
  st: string
  sb: LookupSortKey
  sd: SortDirection
  ps: number
  p: number
  id: string
}

export interface LookupColumn {
  key: LookupSortKey
  label: string
  type?: "text" | "number" | "date" | "status"
}

export interface EodRow {
  trade_date: string
  close: string | number | null
  volume: string | number | null
}

export interface MinuteRow {
  trade_date: string
  bar_start_time: string
  close: string | number
  volume: string | number | null
}

export type FuturesSession = "regular" | "after_hours"

export interface FuturesEodRow {
  contract_id: string
  instrument_id: string
  product_code: string
  contract_code: string
  contract_month: string
  trade_date: string
  session: FuturesSession
  open: string | number | null
  high: string | number | null
  low: string | number | null
  close: string | number | null
  volume: string | number | null
  settlement_price: string | number | null
  open_interest: string | number | null
  source: string
  source_fetched_at: string | null
  asof_ts: string | null
}

export interface FuturesEodFilters {
  productCode: string
  contractCode: string
  startDate: string
  endDate: string
  session: FuturesSession | ""
  cursor: string
}

export interface FuturesEodResponse {
  success: true
  data: FuturesEodRow[]
  pagination: { page_size: number; next_cursor: string | null }
}
