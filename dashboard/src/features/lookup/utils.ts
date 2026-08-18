import { DEFAULT_SEARCH, PAGE_SIZES } from "./config"
import { z } from "zod"
import type {
  DatasetKey,
  LookupColumn,
  LookupItem,
  LookupSearch,
} from "./types"

const INSTRUMENT_SORT_KEYS = new Set([
  "market",
  "symbol",
  "name",
  "asset_class",
  "currency",
  "first_trade_date",
  "latest_trade_date",
  "latest_price",
  "status",
])
const MACRO_SORT_KEYS = new Set([
  "market",
  "source_code",
  "name",
  "frequency",
  "unit",
  "source",
])

const sortKeySchema = z.enum([
  "market",
  "symbol",
  "name",
  "asset_class",
  "currency",
  "first_trade_date",
  "latest_trade_date",
  "latest_price",
  "status",
  "source_code",
  "frequency",
  "unit",
  "source",
])

export const lookupSearchSchema = z
  .object({
    ds: z
      .enum(["instruments", "macro"])
      .catch("instruments")
      .default("instruments"),
    q: z.string().catch("").default(""),
    m: z.string().catch("ALL").default("ALL"),
    ac: z.string().catch("ALL").default("ALL"),
    st: z.string().catch(DEFAULT_SEARCH.st).default(DEFAULT_SEARCH.st),
    fq: z.string().catch("ALL").default("ALL"),
    src: z.string().catch("ALL").default("ALL"),
    sb: sortKeySchema.catch("market").default("market"),
    sd: z.enum(["asc", "desc"]).catch("asc").default("asc"),
    ps: z.coerce
      .number()
      .refine(
        value => PAGE_SIZES.includes(value as (typeof PAGE_SIZES)[number]),
        "Unsupported page size"
      )
      .catch(DEFAULT_SEARCH.ps)
      .default(DEFAULT_SEARCH.ps),
    p: z.coerce.number().int().positive().catch(1).default(1),
    id: z.string().catch("").default(""),
  })
  .transform(values => ({
    ...values,
    st: values.ds === "macro" ? "ALL" : values.st,
    sb: (values.ds === "macro" ? MACRO_SORT_KEYS : INSTRUMENT_SORT_KEYS).has(
      values.sb
    )
      ? values.sb
      : "market",
  }))

export function parseLookupSearch(raw: Record<string, unknown>): LookupSearch {
  return lookupSearchSchema.parse(raw)
}

export function buildPageList(
  currentPage: number,
  totalPages: number,
  sibling = 2
): Array<number | "…"> {
  if (totalPages <= 9) {
    return Array.from({ length: totalPages }, (_, index) => index + 1)
  }
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

export function buildCsv(items: LookupItem[], columns: LookupColumn[]) {
  const header = columns.map(column => escapeCsvCell(column.label)).join(",")
  const rows = items.map(item =>
    columns
      .map(column => {
        const record = item as unknown as Record<string, unknown>
        return escapeCsvCell(record[column.key])
      })
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
    st: dataset === "instruments" ? (persisted?.st ?? "active") : "ALL",
    sb: persisted?.sb ?? "market",
    q: "",
    p: 1,
    id: "",
  }
}

export function formatCell(item: LookupItem, column: LookupColumn) {
  const record = item as unknown as Record<string, unknown>
  const raw = record[column.key]
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
