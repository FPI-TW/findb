import { afterEach, describe, expect, it, vi } from "vitest"

import { DEFAULT_SEARCH } from "./config"
import {
  loadAllLookupItems,
  loadEod,
  loadFuturesEod,
  loadLookupPage,
  loadMinute,
} from "./data"

afterEach(() => vi.restoreAllMocks())

function response(payload: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 503,
    json: vi.fn().mockResolvedValue(payload),
  } as unknown as Response
}

function lookupPayload(page = 1, totalPages = 1) {
  return {
    success: true,
    data: [
      {
        instrument_id: `instrument-${page}`,
        market: "TW",
        symbol: "2330",
        name: "台積電",
        asset_class: "equity",
        currency: "TWD",
        timezone: "Asia/Taipei",
        status: "active",
        listed_date: null,
        delisted_date: null,
        coverage: { eod: null, minute: null },
      },
    ],
    pagination: {
      page,
      page_size: 50,
      total_records: totalPages,
      total_pages: totalPages,
    },
    facets: {
      markets: ["TW"],
      asset_classes: ["equity"],
      statuses: ["active"],
    },
  }
}

describe("lookup API client", () => {
  it("requests the canonical instruments endpoint with NFKC search", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(response(lookupPayload()))
    await loadLookupPage({
      ...DEFAULT_SEARCH,
      q: "２３３０",
      m: "TW",
      ac: "equity",
      sb: "eod_latest_close",
      sd: "desc",
    })

    const url = String(fetchMock.mock.calls[0]?.[0])
    expect(
      url.startsWith("http://localhost:8080/api/v1/serve/instruments?")
    ).toBe(true)
    expect(Object.fromEntries(new URL(url).searchParams)).toEqual({
      q: "2330",
      market: "TW",
      asset_class: "equity",
      status: "active",
      sort_by: "eod_latest_close",
      sort_dir: "desc",
      page: "1",
      page_size: "50",
    })
  })

  it("fetches CSV pages in chunks accepted by the API", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(lookupPayload(1, 2)))
      .mockResolvedValueOnce(response(lookupPayload(2, 2)))
    expect(await loadAllLookupItems(DEFAULT_SEARCH)).toHaveLength(2)
    const urls = fetchMock.mock.calls.map(([url]) => String(url))
    expect(urls.every(url => url.includes("page_size=100"))).toBe(true)
  })

  it("uses the new EOD and minute endpoints without client-side credentials", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(response({ data: [] }))
    await loadEod("instrument/id")
    await loadMinute("instrument/id")
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://localhost:8080/api/v1/serve/eod?instrument_id=instrument%2Fid&page_size=10",
      "http://localhost:8080/api/v1/serve/minute?instrument_id=instrument%2Fid&page_size=10",
    ])
    for (const [, init] of fetchMock.mock.calls) {
      expect(init).not.toHaveProperty("headers.X-API-Key")
    }
  })

  it("queries futures by product, contract, date, session and cursor without credentials", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response({
        success: true,
        data: [],
        pagination: { page_size: 20, next_cursor: null },
      })
    )
    await loadFuturesEod({
      productCode: "TX",
      contractCode: "tx:202610",
      startDate: "2026-10-01",
      endDate: "2026-10-02",
      session: "after_hours",
      cursor: "next/page",
    })
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(new URL(String(url)).pathname).toBe("/api/v1/serve/futures/eod")
    expect(Object.fromEntries(new URL(String(url)).searchParams)).toEqual({
      product_code: "TX",
      contract_code: "TX:202610",
      start_date: "2026-10-01",
      end_date: "2026-10-02",
      session: "after_hours",
      cursor: "next/page",
      page_size: "20",
    })
    expect(JSON.stringify(init)).not.toContain("API-Key")
  })

  it("rejects non-success and malformed list responses", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({}, false))
      .mockResolvedValueOnce(response({ success: true, data: [] }))
    await expect(loadLookupPage(DEFAULT_SEARCH)).rejects.toThrow("HTTP 503")
    await expect(loadLookupPage(DEFAULT_SEARCH)).rejects.toThrow(
      "Lookup API 回應格式不正確"
    )
  })
})
