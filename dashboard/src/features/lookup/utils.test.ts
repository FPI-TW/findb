import { describe, expect, it } from "vitest"

import { DATASET_CONFIG } from "./config"
import type { Instrument } from "./types"
import { buildCsv, buildPageList, parseLookupSearch } from "./utils"

const INSTRUMENT: Instrument = {
  instrument_id: "instrument-tsm",
  market: "TW",
  asset_class: "equity",
  symbol: "2330",
  name: '台積電, "晶圓"',
  currency: "TWD",
  status: "active",
  first_trade_date: "1994-09-05",
  latest_trade_date: "2026-07-22",
  latest_price: "1085.5",
}

describe("lookup utilities", () => {
  it("defaults instruments to the active status filter", () => {
    expect(parseLookupSearch({})).toMatchObject({
      ds: "instruments",
      st: "active",
      sb: "market",
      sd: "asc",
      p: 1,
      ps: 50,
    })
  })

  it("validates pagination, dataset, and supported sort values", () => {
    expect(
      parseLookupSearch({
        ds: "macro",
        ps: "100",
        p: "3",
        sd: "desc",
        sb: "source_code",
        id: "series-cpi",
      })
    ).toMatchObject({
      ds: "macro",
      ps: 100,
      p: 3,
      sd: "desc",
      sb: "source_code",
      id: "series-cpi",
      st: "ALL",
    })
    expect(
      parseLookupSearch({ ds: "macro", ps: "17", p: "-2", sb: "latest_price" })
    ).toMatchObject({ ps: 50, p: 1, sb: "market" })
  })

  it("exports rows as CSV with a BOM and escaped cells", () => {
    const csv = buildCsv([INSTRUMENT], DATASET_CONFIG.instruments.columns)
    expect(csv.startsWith("\uFEFF")).toBe(true)
    expect(csv).toContain('"台積電, ""晶圓"""')
    expect(csv).toContain("2330")
  })

  it("keeps boundary pages and ellipses in long pagination", () => {
    expect(buildPageList(25, 51)).toEqual([1, "…", 23, 24, 25, 26, 27, "…", 51])
  })
})
