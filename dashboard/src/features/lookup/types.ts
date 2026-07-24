export type DatasetKey = "instruments" | "macro"
export type SortDirection = "asc" | "desc"
export type LookupSortKey =
  | "market"
  | "symbol"
  | "name"
  | "asset_class"
  | "currency"
  | "first_trade_date"
  | "latest_trade_date"
  | "latest_price"
  | "status"
  | "source_code"
  | "frequency"
  | "unit"
  | "source"

export interface Instrument {
  instrument_id: string
  market: string | null
  asset_class: string | null
  symbol: string | null
  name: string | null
  currency: string | null
  status: string | null
  first_trade_date: string | null
  latest_trade_date: string | null
  latest_price: string | number | null
}

export interface MacroSeries {
  series_id: string
  name: string | null
  unit: string | null
  frequency: string | null
  market: string | null
  source_code: string | null
  source: string | null
}

export type LookupItem = Instrument | MacroSeries

export interface LookupPagination {
  page: number
  page_size: number
  total_records: number
  total_pages: number
  next_cursor?: string | null
}

export interface InstrumentFacets {
  markets: string[]
  asset_classes: string[]
  statuses: string[]
}

export interface MacroFacets {
  markets: string[]
  frequencies: string[]
  sources: string[]
}

export interface InstrumentLookupResponse {
  success: boolean
  data: Instrument[]
  pagination: LookupPagination
  facets: InstrumentFacets
}

export interface MacroLookupResponse {
  success: boolean
  data: MacroSeries[]
  pagination: LookupPagination
  facets: MacroFacets
}

export type LookupResponse = InstrumentLookupResponse | MacroLookupResponse

export interface LookupSearch {
  ds: DatasetKey
  q: string
  m: string
  ac: string
  st: string
  fq: string
  src: string
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

export interface PriceRow {
  trade_date?: string | null
  close?: string | number | null
  volume?: string | number | null
}

export interface CorporateActionRow {
  ex_date?: string | null
  action_type?: string | null
  cash_amount?: string | number | null
  currency?: string | null
}

export interface MacroObservationRow {
  obs_date?: string | null
  value?: string | number | null
}

export type DetailFeedState<T> =
  | { status: "loading"; data: T[] }
  | { status: "ready"; data: T[] }
  | { status: "error"; data: T[]; message: string }
