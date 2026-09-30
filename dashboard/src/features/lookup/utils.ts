import { z } from "zod"

import { DEFAULT_SEARCH, PAGE_SIZES } from "./config"
import type {
  DatasetKey,
  Instrument,
  LookupColumn,
  LookupSearch,
} from "./types"

const sortKeySchema = z.enum([
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
])

export const lookupSearchSchema = z.object({
  // `.catch()` intentionally normalizes old `?ds=macro` bookmarks.
  ds: z.literal("instruments").catch("instruments").default("instruments"),
  q: z.string().catch("").default(""),
  m: z.string().catch("ALL").default("ALL"),
  ac: z.string().catch("ALL").default("ALL"),
  st: z.string().catch(DEFAULT_SEARCH.st).default(DEFAULT_SEARCH.st),
  sb: sortKeySchema.catch("market").default("market"),
  sd: z.enum(["asc", "desc"]).catch("asc").default("asc"),
  ps: z.coerce
    .number()
    .refine(value => PAGE_SIZES.includes(value as (typeof PAGE_SIZES)[number]))
    .catch(DEFAULT_SEARCH.ps)
    .default(DEFAULT_SEARCH.ps),
  p: z.coerce.number().int().positive().catch(1).default(1),
  id: z.string().catch("").default(""),
})

export function parseLookupSearch(raw: Record<string, unknown>): LookupSearch {
  if (raw.ds === "macro") return { ...DEFAULT_SEARCH }
  return lookupSearchSchema.parse(raw)
}

export function buildPageList(
  currentPage: number,
  totalPages: number,
  sibling = 2
): Array<number | "…"> {
  if (totalPages <= 9)
    return Array.from({ length: totalPages }, (_, index) => index + 1)
  const start = Math.max(2, Math.min(currentPage - sibling, totalPages - 5))
  const end = Math.min(totalPages - 1, Math.max(currentPage + sibling, 6))
  const pages: Array<number | "…"> = [1]
  if (start > 2) pages.push("…")
  for (let page = start; page <= end; page += 1) pages.push(page)
  if (end < totalPages - 1) pages.push("…")
  pages.push(totalPages)
  return pages
}

function escapeCsvCell(value: unknown) {
  const text = String(value ?? "")
  if (!/[",\r\n]/.test(text)) return text
  return `"${text.replaceAll('"', '""')}"`
}

function columnValue(item: Instrument, key: LookupColumn["key"]): unknown {
  if (key.startsWith("eod_")) {
    return item.coverage.eod?.[
      key.slice(4) as keyof NonNullable<Instrument["coverage"]["eod"]>
    ]
  }
  if (key.startsWith("minute_")) {
    return item.coverage.minute?.[
      key.slice(7) as keyof NonNullable<Instrument["coverage"]["minute"]>
    ]
  }
  return item[key as keyof Instrument]
}

export function buildCsv(items: Instrument[], columns: LookupColumn[]) {
  const header = columns.map(column => escapeCsvCell(column.label)).join(",")
  const rows = items.map(item =>
    columns
      .map(column => escapeCsvCell(columnValue(item, column.key)))
      .join(",")
  )
  return `\uFEFF${[header, ...rows].join("\r\n")}`
}

export function nextDatasetSearch(
  dataset: DatasetKey,
  persisted?: Partial<LookupSearch>
): LookupSearch {
  return {
    ...DEFAULT_SEARCH,
    ...persisted,
    ds: dataset,
    q: "",
    p: 1,
    id: "",
  }
}

export function formatCell(item: Instrument, column: LookupColumn) {
  const raw = columnValue(item, column.key)
  if (raw === null || raw === undefined || raw === "") return "—"
  if (column.type === "number") {
    const value = Number(raw)
    return Number.isFinite(value)
      ? new Intl.NumberFormat("en-US", { maximumFractionDigits: 8 }).format(
          value
        )
      : "—"
  }
  return String(raw)
}
