import { describe, expect, it } from "vitest"

import {
  auditFiltersSchema,
  buildAuditSearch,
  dashboardRequestSchema,
  dqIssuesSchema,
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
      datasetKey: "tw_equity_eod",
      runId: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
      dateFrom: "2026-01-01",
      dateTo: "2026-01-31",
      page: 3,
      pageSize: 100,
    })
    const params = buildAuditSearch(filters)
    expect(params.get("page")).toBe("3")
    expect(params.get("page_size")).toBe("100")
    expect(params.get("dataset_key")).toBe("tw_equity_eod")
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
      slot_id: "western_markets_window",
      scheduled_local_time: "06:30:00",
      timezone: "Asia/Taipei",
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

  it("keeps DQ provenance and bounded policy details without raw payloads", () => {
    const parsed = dqIssuesSchema.parse({
      data: [
        {
          id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
          run_id: "019565d2-f838-7c91-85c1-72d4d7bbbe98",
          instrument_id: null,
          trade_date: "2026-08-04",
          issue_type: "price_gap",
          severity: "warning",
          description: "close differs from provider policy",
          provider: "finlab",
          source: "finlab",
          dataset_key: "tw_equity_eod",
          schema_id: "market_eod",
          schema_version: 1,
          raw_payload_id: null,
          raw_available: true,
          fetched_at: "2026-08-04T02:00:00Z",
          request_key: "request-1",
          batch_data_date: "2026-08-04",
          policy_detail: {
            code: "close_gap",
            action: "review",
            violations: [{ observed: 101, expected: 100 }],
            violation_count: 1,
            truncated: false,
          },
          raw_data: { secret: "must never cross the API boundary" },
          resolved: false,
          created_at: "2026-08-04T02:01:00Z",
        },
      ],
      pagination: {
        page: 1,
        page_size: 25,
        total_records: 1,
        total_pages: 1,
      },
    })

    const issue = parsed.data[0]
    expect(issue?.provider).toBe("finlab")
    expect(issue?.dataset_key).toBe("tw_equity_eod")
    expect(issue?.policy_detail?.code).toBe("close_gap")
    expect(issue).not.toHaveProperty("raw_data")
  })
})
