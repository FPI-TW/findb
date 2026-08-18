import { afterEach, describe, expect, it, vi } from "vitest"

import { DEFAULT_SEARCH } from "./config"
import {
  loadAllLookupItems,
  loadCorporateActions,
  loadLookupPage,
  loadMacroObservations,
  loadPrices,
} from "./data"

afterEach(() => {
  vi.restoreAllMocks()
})

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
        status: "active",
        first_trade_date: "1994-09-05",
        latest_trade_date: "2026-07-22",
        latest_price: "1085.5",
      },
    ],
    pagination: {
      page,
      page_size: 50,
      total_records: totalPages,
      total_pages: totalPages,
      next_cursor: null,
    },
    facets: {
      markets: ["TW"],
      asset_classes: ["equity"],
      statuses: ["active"],
    },
  }
}

describe("lookup API client", () => {
  it("requests the instrument lookup contract with active as the default", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(response(lookupPayload()))

    await loadLookupPage({
      ...DEFAULT_SEARCH,
      q: "２３３０",
      m: "TW",
      ac: "equity",
      sb: "latest_price",
      sd: "desc",
    })

    const url = String(fetchMock.mock.calls[0]?.[0])
    expect(
      url.startsWith("http://localhost:8080/api/v1/serve/lookup/instruments?")
    ).toBe(true)
    const params = new URL(url, "https://findb.example").searchParams
    expect(Object.fromEntries(params)).toEqual({
      q: "2330",
      market: "TW",
      asset_class: "equity",
      status: "active",
      sort_by: "latest_price",
      sort_dir: "desc",
      page: "1",
      page_size: "50",
    })
  })

  it("passes React Query cancellation signals through to the lookup fetch", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(response(lookupPayload()))
    const controller = new AbortController()

    await loadLookupPage(DEFAULT_SEARCH, controller.signal)

    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      signal: controller.signal,
    })
  })

  it("omits cleared filters from the macro lookup request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response({
        success: true,
        data: [],
        pagination: {
          page: 2,
          page_size: 25,
          total_records: 0,
          total_pages: 0,
        },
        facets: { markets: [], frequencies: [], sources: [] },
      })
    )

    await loadLookupPage({
      ...DEFAULT_SEARCH,
      ds: "macro",
      st: "ALL",
      p: 2,
      ps: 25,
      q: " ",
    })

    const url = String(fetchMock.mock.calls[0]?.[0])
    expect(
      url.startsWith("http://localhost:8080/api/v1/serve/lookup/macro-series?")
    ).toBe(true)
    const params = new URL(url, "https://findb.example").searchParams
    expect(params.has("q")).toBe(false)
    expect(params.has("market")).toBe(false)
    expect(params.has("frequency")).toBe(false)
    expect(params.has("source")).toBe(false)
    expect(params.has("status")).toBe(false)
  })

  it("fetches every filtered page in 200-row chunks for CSV", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(lookupPayload(1, 2)))
      .mockResolvedValueOnce(response(lookupPayload(2, 2)))

    const items = await loadAllLookupItems({
      ...DEFAULT_SEARCH,
      m: "TW",
    })

    expect(items).toHaveLength(2)
    const urls = fetchMock.mock.calls.map(([url]) => String(url))
    expect(urls.every(url => url.includes("page_size=200"))).toBe(true)
    expect(urls[0]).toContain("page=1")
    expect(urls[1]).toContain("page=2")
  })

  it("keeps detail reads on the fixed Serve endpoints", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(response({ data: [] }))

    await loadPrices("instrument/id")
    await loadCorporateActions("instrument/id")
    await loadMacroObservations("series/id")

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://localhost:8080/api/v1/serve/eod/instrument%2Fid?page_size=10",
      "http://localhost:8080/api/v1/serve/corporate-actions/instrument%2Fid?page_size=5",
      "http://localhost:8080/api/v1/serve/macro/observations/series%2Fid?page_size=10",
    ])
    for (const [, init] of fetchMock.mock.calls) {
      expect(init).not.toHaveProperty("headers.X-API-Key")
    }
  })

  it("rejects non-success responses and malformed payloads", async () => {
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({}, false))
      .mockResolvedValueOnce(response({ success: true, data: [] }))

    await expect(loadLookupPage(DEFAULT_SEARCH)).rejects.toThrow("HTTP 503")
    await expect(loadLookupPage(DEFAULT_SEARCH)).rejects.toThrow(
      "Lookup API 回應格式不正確"
    )
  })
})
