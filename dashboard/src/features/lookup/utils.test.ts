import { describe, expect, it } from "vitest"

import { DEFAULT_SEARCH, LOOKUP_COLUMNS } from "./config"
import type { Instrument } from "./types"
import { buildCsv, buildPageList, formatCell, parseLookupSearch } from "./utils"

const INSTRUMENT: Instrument = {
  instrument_id: "instrument-tsm",
  market: "TW",
  asset_class: "equity",
  symbol: "2330",
  name: '台積電, "晶圓"',
  currency: "TWD",
  timezone: "Asia/Taipei",
  status: "active",
  listed_date: "1994-09-05",
  delisted_date: null,
  coverage: {
    eod: {
      first_date: "1994-09-05",
      latest_date: "2026-07-22",
      latest_close: "1085.5",
    },
    minute: null,
  },
}

describe("lookup utilities", () => {
  it("canonicalizes defaults and old macro bookmarks to instruments", () => {
    expect(parseLookupSearch({})).toEqual(DEFAULT_SEARCH)
    expect(
      parseLookupSearch({
        ds: "macro",
        fq: "monthly",
        src: "legacy",
        q: "CPI",
        m: "US",
        ac: "macro",
        st: "ALL",
        sb: "name",
        sd: "desc",
        ps: "100",
        p: "4",
        id: "legacy-series-id",
      })
    ).toEqual(DEFAULT_SEARCH)
  })

  it("validates pagination and current sort values", () => {
    expect(
      parseLookupSearch({
        ps: "100",
        p: "3",
        sd: "desc",
        sb: "minute_latest_close",
      })
    ).toMatchObject({
      ps: 100,
      p: 3,
      sd: "desc",
      sb: "minute_latest_close",
    })
    expect(
      parseLookupSearch({ ps: "200", p: "-2", sb: "source_code" })
    ).toMatchObject({
      ps: 50,
      p: 1,
      sb: "market",
    })
  })

  it("exports nested coverage as CSV with escaped cells", () => {
    const csv = buildCsv([INSTRUMENT], LOOKUP_COLUMNS)
    expect(csv.startsWith("\uFEFF")).toBe(true)
    expect(csv).toContain('"台積電, ""晶圓"""')
    expect(csv).toContain("1994-09-05")
    expect(csv).toContain("1085.5")
  })

  it("preserves zero-padded HK symbols and currency in display and CSV", () => {
    const hk: Instrument = {
      ...INSTRUMENT,
      market: "HK",
      symbol: "00700",
      currency: "HKD",
    }
    expect(
      formatCell(
        hk,
        LOOKUP_COLUMNS.find(column => column.key === "symbol")!
      )
    ).toBe("00700")
    expect(buildCsv([hk], LOOKUP_COLUMNS)).toContain("HK,00700")
    expect(buildCsv([hk], LOOKUP_COLUMNS)).toContain("HKD")
  })

  it("keeps boundary pages and ellipses in long pagination", () => {
    expect(buildPageList(25, 51)).toEqual([1, "…", 23, 24, 25, 26, 27, "…", 51])
  })
})
