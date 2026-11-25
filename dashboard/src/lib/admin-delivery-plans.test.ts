import { describe, expect, it, vi } from "vitest"

import {
  completenessRequestSchema,
  deliveryPlanSummaryRequestSchema,
  historicalBackfillItemsRequestSchema,
} from "./admin-api"
import {
  fetchCompletenessData,
  fetchDeliveryPlanSummaryData,
  fetchHistoricalBackfillItemsData,
} from "./admin.server"

const summary = {
  expected: 5,
  data: 2,
  no_data: 1,
  missing: 1,
  blocked: 1,
  deadline_at: "2026-10-01T15:00:00Z",
  is_late: true,
  gaps: [
    { member_key: "TX", status: "missing", reason: "pending" },
    { member_key: "MTX", status: "blocked", reason: "quota" },
  ],
  gaps_total: 2,
}

const matrix = {
  execution_profile: "full_market",
  date_from: "2026-10-01",
  date_to: "2026-10-02",
  evaluated_at: "2026-10-02T12:00:00Z",
  rows: [
    {
      dataset_key: "tw_futures_eod",
      provider: "taifex",
      market: "TAIFEX",
      first_start_date: "2026-09-01",
      latest_due_date: "2026-10-02",
      cells: [
        {
          trade_date: "2026-10-01",
          state: "late",
          plan_id: "0195a000-0000-7000-8000-000000000001",
          deadline_at: "2026-10-01T15:00:00Z",
          counts: {
            expected: 5,
            data: 2,
            no_data: 1,
            missing: 1,
            blocked: 1,
            unavailable: 0,
            mapping_gap: 0,
            unlisted_data: 0,
          },
          counts_synced: true,
          open_alert: true,
        },
        {
          trade_date: "2026-10-02",
          state: "not_planned",
          plan_id: null,
          deadline_at: null,
          counts: null,
          counts_synced: true,
          open_alert: false,
        },
      ],
    },
  ],
}

function respond(body: unknown, status = 200) {
  return vi
    .fn()
    .mockResolvedValue(new Response(JSON.stringify(body), { status }))
}

describe("completeness session proxy", () => {
  it("asks for stored counts with the session token and the chosen range", async () => {
    const fetchMock = respond(matrix)
    const payload = await fetchCompletenessData(
      completenessRequestSchema.parse({
        dateFrom: "2026-10-01",
        dateTo: "2026-10-02",
      }),
      "session-token",
      "https://api.example.test",
      fetchMock
    )
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(String(url)).toBe(
      "https://api.example.test/api/v1/admin/completeness?execution_profile=full_market&date_from=2026-10-01&date_to=2026-10-02"
    )
    expect(init.headers).toEqual({
      Authorization: "Bearer session-token",
      Accept: "application/json",
    })
    expect(init.cache).toBe("no-store")
    expect(payload.rows[0]?.cells.map(cell => cell.state)).toEqual([
      "late",
      "not_planned",
    ])
  })

  it("leaves the range to the server when none is chosen", async () => {
    const fetchMock = respond(matrix)
    await fetchCompletenessData(
      completenessRequestSchema.parse({}),
      "session-token",
      "https://api.example.test",
      fetchMock
    )
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "https://api.example.test/api/v1/admin/completeness?execution_profile=full_market"
    )
  })

  it("rejects API errors rather than showing an empty matrix", async () => {
    await expect(
      fetchCompletenessData(
        completenessRequestSchema.parse({}),
        "session-token",
        "https://api.example.test",
        vi.fn().mockResolvedValue(new Response("error", { status: 503 }))
      )
    ).rejects.toThrow("503")
  })
})

describe("plan gap details", () => {
  it("reads one plan's gaps filtered by status and paged", async () => {
    const fetchMock = respond(summary)
    const planId = "0195a000-0000-7000-8000-000000000001"
    const payload = await fetchDeliveryPlanSummaryData(
      deliveryPlanSummaryRequestSchema.parse({
        planId,
        gapStatus: ["missing", "blocked"],
        gapPage: 2,
        gapPageSize: 200,
      }),
      "session-token",
      "https://api.example.test",
      fetchMock
    )
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      `https://api.example.test/api/v1/admin/delivery-plans/${planId}/summary?gap_page=2&gap_page_size=200&gap_status=missing&gap_status=blocked`
    )
    expect(payload.gaps).toEqual(summary.gaps)
    expect(payload.gaps_total).toBe(2)
  })

  it("only accepts a plan id that cannot change the request path", () => {
    for (const planId of ["", "p1", "../schedulers", "a/b", "plan-1?x=1"])
      expect(
        deliveryPlanSummaryRequestSchema.safeParse({ planId }).success
      ).toBe(false)
    expect(
      deliveryPlanSummaryRequestSchema.parse({
        planId: "0195a000-0000-7000-8000-000000000001",
      })
    ).toMatchObject({ gapStatus: [], gapPage: 1, gapPageSize: 1000 })
  })
})

describe("historical backfill items", () => {
  it("loads one request's dates on demand", async () => {
    const requestId = "0195a000-0000-7000-8000-000000000002"
    const fetchMock = respond({
      data: [
        {
          item_id: "0195a000-0000-7000-8000-000000000003",
          trade_date: "2026-10-01",
          status: "completed",
          run_id: null,
          failure_code: null,
          failure_message: null,
          unlisted_symbols: null,
        },
      ],
      pagination: { page: 1, page_size: 500, total_records: 1, total_pages: 1 },
    })
    const payload = await fetchHistoricalBackfillItemsData(
      historicalBackfillItemsRequestSchema.parse({ requestId }),
      "session-token",
      "https://api.example.test",
      fetchMock
    )
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      `https://api.example.test/api/v1/admin/historical-backfills/${requestId}/items?page=1&page_size=500`
    )
    expect(payload.data).toHaveLength(1)
    expect(
      historicalBackfillItemsRequestSchema.safeParse({ requestId: "../x" })
        .success
    ).toBe(false)
  })
})
