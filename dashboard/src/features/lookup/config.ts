import type { DatasetKey, LookupColumn, LookupSearch } from "./types"

export const PAGE_SIZES = [25, 50, 100, 200] as const
export const LOOKUP_STORAGE_KEY = "findb.lookup.preferences.v2"

export const DEFAULT_SEARCH: LookupSearch = {
  ds: "instruments",
  q: "",
  m: "ALL",
  ac: "ALL",
  st: "active",
  fq: "ALL",
  src: "ALL",
  sb: "market",
  sd: "asc",
  ps: 50,
  p: 1,
  id: "",
}

export const DATASET_CONFIG: Record<
  DatasetKey,
  {
    label: string
    itemLabel: string
    searchPlaceholder: string
    columns: LookupColumn[]
  }
> = {
  instruments: {
    label: "金融商品",
    itemLabel: "標的",
    searchPlaceholder: "標的名稱、Symbol、Instrument ID…",
    columns: [
      { key: "market", label: "Market" },
      { key: "symbol", label: "Symbol" },
      { key: "name", label: "Name" },
      { key: "asset_class", label: "Asset Class" },
      { key: "currency", label: "CCY" },
      { key: "first_trade_date", label: "First Date", type: "date" },
      { key: "latest_trade_date", label: "Last Date", type: "date" },
      { key: "latest_price", label: "Last Price", type: "number" },
      { key: "status", label: "Status", type: "status" },
    ],
  },
  macro: {
    label: "宏觀序列",
    itemLabel: "宏觀序列",
    searchPlaceholder: "宏觀序列名稱、Source Code、Series ID…",
    columns: [
      { key: "market", label: "Market" },
      { key: "source_code", label: "Source Code" },
      { key: "name", label: "Name" },
      { key: "frequency", label: "Frequency" },
      { key: "unit", label: "Unit" },
      { key: "source", label: "Source" },
    ],
  },
}
