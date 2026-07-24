import { describe, expect, it } from "vitest"

import {
  auditFiltersSchema,
  buildAuditSearch,
  dashboardRequestSchema,
} from "./admin-api"

describe("admin API request helpers", () => {
  it("omits empty audit filters", () => {
    const filters = auditFiltersSchema.parse({})
    expect(buildAuditSearch(filters).toString()).toBe("page=1&page_size=25")
  })

  it("encodes supported audit filters and pagination", () => {
    const filters = auditFiltersSchema.parse({
      datasetKey: "tw.eod",
      runId: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
      dateFrom: "2026-01-01",
      dateTo: "2026-01-31",
      page: 3,
      pageSize: 100,
    })
    const params = buildAuditSearch(filters)
    expect(params.get("page")).toBe("3")
    expect(params.get("page_size")).toBe("100")
    expect(params.get("dataset_key")).toBe("tw.eod")
    expect(params.get("run_id")).toBe("019565d2-f838-7c91-85c1-72d4d7bbbe97")
    expect(params.get("date_from")).toBe("2026-01-01")
    expect(params.get("date_to")).toBe("2026-01-31")
  })

  it("rejects page sizes above the Dashboard limit", () => {
    expect(
      auditFiltersSchema.safeParse({ page: 1, pageSize: 101 }).success
    ).toBe(false)
  })

  it("accepts audit input without exposing an Admin API key field", () => {
    const parsed = dashboardRequestSchema.parse({
      audit: auditFiltersSchema.parse({}),
    })
    expect(parsed).toEqual({ audit: auditFiltersSchema.parse({}) })
    expect(parsed).not.toHaveProperty("apiKey")
  })
})
