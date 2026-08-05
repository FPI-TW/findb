import { describe, expect, it } from "vitest"

import {
  auditFiltersSchema,
  buildAuditSearch,
  dashboardRequestSchema,
  schedulerMutationRequestSchema,
  schedulerSchema,
  schedulersResponseSchema,
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

  it("validates scheduler rows and owner mutation input", () => {
    const timestamp = "2026-07-28T02:00:00Z"
    const scheduler = schedulerSchema.parse({
      scheduler_key: "twelve_data_us_common_stocks_daily_v1",
      provider: "twelve_data",
      dataset_keys: ["us_equity_eod"],
      desired_state: "running",
      observed_state: "running",
      revision: 2,
      last_heartbeat_at: timestamp,
      last_cycle_started_at: timestamp,
      last_cycle_completed_at: null,
      last_error: null,
      created_at: timestamp,
      updated_at: timestamp,
      heartbeat_age_seconds: 5,
    })
    expect(
      schedulersResponseSchema.parse({ success: true, data: [scheduler] }).data
    ).toHaveLength(1)
    expect(
      schedulerMutationRequestSchema.parse({
        schedulerKey: scheduler.scheduler_key,
        desiredState: "stopped",
        expectedRevision: scheduler.revision,
      }).desiredState
    ).toBe("stopped")
    expect(
      schedulerMutationRequestSchema.safeParse({
        schedulerKey: scheduler.scheduler_key,
        desiredState: "paused",
        expectedRevision: scheduler.revision,
      }).success
    ).toBe(false)
  })
})
