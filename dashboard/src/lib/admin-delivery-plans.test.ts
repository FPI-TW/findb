import { describe, expect, it, vi } from "vitest"

import { fetchDeliveryPlansData } from "./admin.server"

const plan = {
  plan_id: "plan-1",
  dataset_key: "tw_futures_eod",
  provider: "shioaji",
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
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ data: [plan] }), { status: 200 })
      )
    const payload = await fetchDeliveryPlansData(
      { datasetKey: "tw_futures_eod", tradeDate: "2026-10-01", limit: 25 },
      "session-token",
      "https://api.example.test",
      fetchMock
    )
    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(String(url)).toBe(
      "https://api.example.test/api/v1/admin/delivery-plans?limit=25&dataset_key=tw_futures_eod&trade_date=2026-10-01"
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
        { datasetKey: "", tradeDate: "", limit: 25 },
        "session-token",
        "https://api.example.test",
        fetchMock
      )
    ).rejects.toThrow("503")
  })
})
