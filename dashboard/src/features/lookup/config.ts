import type { LookupColumn, LookupSearch } from "./types"

export const PAGE_SIZES = [25, 50, 100] as const
export const LOOKUP_STORAGE_KEY = "findb.lookup.preferences.v3"

export const DEFAULT_SEARCH: LookupSearch = {
  ds: "instruments",
  q: "",
  m: "ALL",
  ac: "ALL",
  st: "active",
  sb: "market",
  sd: "asc",
  ps: 50,
  p: 1,
  id: "",
}

export const LOOKUP_COLUMNS: LookupColumn[] = [
  { key: "market", label: "Market" },
  { key: "symbol", label: "Symbol" },
  { key: "name", label: "Name" },
  { key: "asset_class", label: "Asset Class" },
  { key: "currency", label: "CCY" },
  { key: "eod_first_date", label: "EOD First", type: "date" },
  { key: "eod_latest_date", label: "EOD Latest", type: "date" },
  { key: "eod_latest_close", label: "EOD Close", type: "number" },
  { key: "minute_first_bar_at", label: "Minute First", type: "date" },
  { key: "minute_latest_bar_at", label: "Minute Latest", type: "date" },
  { key: "minute_latest_close", label: "Minute Close", type: "number" },
  { key: "status", label: "Status", type: "status" },
]
