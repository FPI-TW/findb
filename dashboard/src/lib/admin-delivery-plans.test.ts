import { describe, expect, it, vi } from "vitest"

import {
  fetchDeliveryPlanSummaryData,
  fetchDeliveryPlansData,
} from "./admin.server"

const plan = {
  plan_id: "plan-1",
  dataset_key: "tw_futures_eod",
  provider: "taifex",
  trade_date: "2026-10-01",
  release_id: "release-1",
  deadline_at: "2026-10-01T15:00:00Z",
  status: "incomplete",
  parts: [],
  summary: {
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
  },
}

describe("delivery plan session proxy", () => {
  it("uses the session bearer token and bounded filters while preserving no_data and gaps", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          data: [plan],
          pagination: {
            page: 1,
            page_size: 25,
            total_records: 1,
            total_pages: 1,
          },
        }),
        { status: 200 }
      )
    )
    const payload = await fetchDeliveryPlansData(
      {
        datasetKey: "tw_futures_eod",
        tradeDate: "2026-10-01",
        page: 1,
        pageSize: 25,
      },
      "session-token",
      "https://api.example.test",
      fetchMock
    )
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(String(url)).toBe(
      "https://api.example.test/api/v1/admin/delivery-plans?execution_profile=full_market&page=1&page_size=25&summary_only=true&dataset_key=tw_futures_eod&trade_date=2026-10-01"
    )
    expect(init.headers).toEqual({
      Authorization: "Bearer session-token",
      Accept: "application/json",
    })
    expect(init.cache).toBe("no-store")
    expect(payload.data[0]?.summary).toMatchObject({
      no_data: 1,
      missing: 1,
      blocked: 1,
    })
    expect(payload.data[0]?.summary.gaps).toHaveLength(2)
  })

  it("rejects API errors rather than treating them as an empty complete plan list", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response("error", { status: 503 }))
    await expect(
      fetchDeliveryPlansData(
        { datasetKey: "", tradeDate: "", page: 1, pageSize: 25 },
        "session-token",
        "https://api.example.test",
        fetchMock
      )
    ).rejects.toThrow("503")
  })

  it("reads one plan's gap details from its own summary endpoint", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(plan.summary), { status: 200 })
      )
    const planId = "0195a000-0000-7000-8000-000000000001"
    const summary = await fetchDeliveryPlanSummaryData(
      { planId },
      "session-token",
      "https://api.example.test",
      fetchMock
    )
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(String(url)).toBe(
      `https://api.example.test/api/v1/admin/delivery-plans/${planId}/summary`
    )
    expect(init.headers).toEqual({
      Authorization: "Bearer session-token",
      Accept: "application/json",
    })
    expect(summary.gaps).toEqual(plan.summary.gaps)
  })

  it("only accepts a plan id that cannot change the request path", async () => {
    const { deliveryPlanSummaryRequestSchema } = await import("./admin-api")
    for (const planId of ["", "p1", "../schedulers", "a/b", "plan-1?x=1"])
      expect(
        deliveryPlanSummaryRequestSchema.safeParse({ planId }).success
      ).toBe(false)
    expect(
      deliveryPlanSummaryRequestSchema.safeParse({
        planId: "0195a000-0000-7000-8000-000000000001",
      }).success
    ).toBe(true)
  })
})
