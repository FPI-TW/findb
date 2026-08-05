import { describe, expect, it } from "vitest"

import { type DashboardRequest, mergeDashboardRefresh } from "./admin-api"
import { fetchDashboardData, patchSchedulerData } from "./admin.server"

const timestamp = "2026-07-23T02:00:00Z"
const pagination = {
  page: 1,
  page_size: 10,
  total_records: 1,
  total_pages: 1,
}
const request: DashboardRequest = {
  audit: {
    datasetKey: "tw.eod",
    runId: "",
    dateFrom: "",
    dateTo: "",
    page: 3,
    pageSize: 100,
  },
}

const responses = {
  "/api/v1/admin/market-freshness": {
    success: true,
    data: [
      {
        market: "TW",
        scheduler_key: "finlab_tw_1430_tw_equity_eod",
        provider: "finlab",
        dataset_keys: ["tw_equity_eod"],
        slot_id: "tw_1430",
        scheduled_local_time: "14:30:00",
        timezone: "Asia/Taipei",
        desired_state: "running",
        observed_state: "running",
        revision: 4,
        last_heartbeat_at: timestamp,
        last_cycle_started_at: timestamp,
        last_cycle_completed_at: timestamp,
        last_error: null,
        heartbeat_age_seconds: 2,
        configuration_status: "ready",
        configuration_errors: [],
        status: "partial",
        expected_data_date: "2026-07-23",
        coverage_data_date: "2026-07-22",
        last_fetched_at: timestamp,
        last_successful_update_at: timestamp,
        last_complete_at: null,
        next_scheduled_at: timestamp,
        feed_count: 2,
        fresh_feed_count: 1,
        late_feed_count: 1,
        feeds: [
          {
            dataset_key: "tw_equity_eod",
            source: "finlab",
            schema_id: "market_eod",
            schema_version: 1,
            expected_data_date: "2026-07-23",
            latest_successful_data_date: "2026-07-23",
            last_fetched_at: timestamp,
            last_completed_at: timestamp,
            last_run_id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
            total_records: 2100,
            success_records: 2100,
            failed_records: 0,
            policy_outcome: "pass",
            open_missing_delivery_alert: false,
            last_failure_code: null,
            configuration_error: null,
            status: "fresh",
          },
        ],
      },
    ],
  },
  "/api/v1/admin/queue/health": {
    counts: { queued: 1 },
    oldest_queued_at: timestamp,
    oldest_queued_age_seconds: 10,
    unpublished_outbox: 0,
    oldest_unpublished_outbox_at: null,
    oldest_unpublished_outbox_age_seconds: null,
    expired_leases: 0,
    retry_exhausted: 0,
    last_worker_heartbeat_at: timestamp,
    worker_heartbeat_age_seconds: 2,
    missing_deliveries: 0,
    oldest_missing_delivery_at: null,
    oldest_missing_delivery_age_seconds: null,
  },
  "/api/v1/admin/schedulers": {
    success: true,
    data: [
      {
        scheduler_key: "twelve_data_us_common_stocks_daily_v1",
        provider: "twelve_data",
        dataset_keys: ["us_equity_eod"],
        slot_id: "us_0600",
        scheduled_local_time: "06:00:00",
        timezone: "America/New_York",
        desired_state: "running",
        observed_state: "running",
        revision: 3,
        last_heartbeat_at: timestamp,
        last_cycle_started_at: timestamp,
        last_cycle_completed_at: timestamp,
        last_error: null,
        created_at: timestamp,
        updated_at: timestamp,
        heartbeat_age_seconds: 2,
      },
      {
        scheduler_key: "finlab_tw_1430_tw_equity_eod",
        provider: "finlab",
        dataset_keys: ["tw_equity_eod"],
        slot_id: "tw_1430",
        scheduled_local_time: "14:30:00",
        timezone: "Asia/Taipei",
        desired_state: "stopped",
        observed_state: "stopped",
        revision: 4,
        last_heartbeat_at: null,
        last_cycle_started_at: null,
        last_cycle_completed_at: null,
        last_error: null,
        created_at: timestamp,
        updated_at: timestamp,
        heartbeat_age_seconds: null,
      },
      {
        scheduler_key: "shioaji_tw_pilot_v1",
        provider: "shioaji",
        dataset_keys: ["tw_equity_minute", "tw_etf_minute"],
        slot_id: "tw_1430",
        scheduled_local_time: "14:30:00",
        timezone: "Asia/Taipei",
        desired_state: "stopped",
        observed_state: "running",
        revision: 1,
        last_heartbeat_at: timestamp,
        last_cycle_started_at: timestamp,
        last_cycle_completed_at: null,
        last_error: "provider timeout",
        created_at: timestamp,
        updated_at: timestamp,
        heartbeat_age_seconds: 120,
      },
    ],
  },
  "/api/v1/admin/missing-deliveries": {
    data: [],
    pagination,
  },
  "/api/v1/admin/dq-issues": {
    data: [],
    pagination,
  },
  "/api/v1/admin/corrections": {
    data: [],
    pagination,
  },
  "/api/v1/admin/raw-payloads": {
    data: [
      {
        idempotency_key: "delivery-1",
        run_id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
        dataset_key: "tw.eod",
        source: "bloomberg",
        schema_id: null,
        schema_version: null,
        request_key: "request-1",
        payload: { rows: [{ symbol: "2330", close: 1000 }] },
        fetched_at: timestamp,
        expire_at: timestamp,
        created_at: timestamp,
      },
    ],
    pagination,
  },
} as const

type RecordedCall = { url: URL; init: RequestInit | undefined }

function responseFor(pathname: string) {
  const response = responses[pathname as keyof typeof responses]
  return new Response(JSON.stringify(response), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })
}

function recordingFetch(
  calls: RecordedCall[],
  statusByPath: Partial<Record<keyof typeof responses, number>> = {},
  malformedPath?: keyof typeof responses
): typeof fetch {
  return async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : input.toString())
    calls.push({ url, init })
    const status = statusByPath[url.pathname as keyof typeof responses]
    if (status) {
      return new Response("sensitive upstream body", { status })
    }
    if (url.pathname === malformedPath) {
      return Response.json({ unexpected: "sensitive response value" })
    }
    return responseFor(url.pathname)
  }
}

describe("FinDB Admin server boundary", () => {
  it("calls only fixed GET targets and forwards the session without caching", async () => {
    const calls: RecordedCall[] = []
    const result = await fetchDashboardData(
      request,
      "operator-secret",
      "https://findb.internal:8443",
      recordingFetch(calls)
    )

    expect(calls).toHaveLength(7)
    expect(calls.map(call => call.url.pathname)).toEqual([
      "/api/v1/admin/market-freshness",
      "/api/v1/admin/queue/health",
      "/api/v1/admin/schedulers",
      "/api/v1/admin/missing-deliveries",
      "/api/v1/admin/dq-issues",
      "/api/v1/admin/corrections",
      "/api/v1/admin/raw-payloads",
    ])
    for (const call of calls) {
      expect(call.url.origin).toBe("https://findb.internal:8443")
      expect(call.init?.method).toBe("GET")
      expect(call.init?.cache).toBe("no-store")
      expect(new Headers(call.init?.headers).get("Authorization")).toBe(
        "Bearer operator-secret"
      )
    }
    expect(calls[6]?.url.searchParams.get("dataset_key")).toBe("tw.eod")
    expect(calls[6]?.url.searchParams.get("page")).toBe("3")
    expect(calls[6]?.url.searchParams.get("page_size")).toBe("100")
    expect(calls[4]?.url.searchParams.get("resolved")).toBe("false")
    expect(calls[4]?.url.searchParams.get("page")).toBe("3")
    expect(calls[4]?.url.searchParams.get("page_size")).toBe("100")
    expect(calls[4]?.url.searchParams.has("dataset_key")).toBe(false)
    expect(result.freshness.ok).toBe(true)
    expect(result.rawPayloads.ok).toBe(true)
    expect(result.schedulers.ok).toBe(true)
  })

  it("keeps successful panels when one endpoint fails", async () => {
    const result = await fetchDashboardData(
      request,
      "operator-secret",
      undefined,
      recordingFetch([], { "/api/v1/admin/dq-issues": 503 })
    )

    expect(result.queue.ok).toBe(true)
    expect(result.freshness.ok).toBe(true)
    expect(result.deliveries.ok).toBe(true)
    expect(result.issues).toEqual({
      ok: false,
      error: "FinDB API request failed (503)",
    })
    expect(result.corrections.ok).toBe(true)
    expect(result.rawPayloads.ok).toBe(true)
  })

  it("retains the last successful freshness panel when polling fails", async () => {
    const current = await fetchDashboardData(
      request,
      "operator-secret",
      undefined,
      recordingFetch([])
    )
    const next = await fetchDashboardData(
      request,
      "operator-secret",
      undefined,
      recordingFetch([], { "/api/v1/admin/market-freshness": 503 })
    )

    const merged = mergeDashboardRefresh(current, next)

    expect(merged.data.freshness).toEqual(current.freshness)
    expect(merged.data.queue).toEqual(next.queue)
    expect(merged.freshnessError).toBe("FinDB API request failed (503)")
  })

  it("sanitizes authentication and upstream response bodies", async () => {
    const authResult = await fetchDashboardData(
      request,
      "operator-secret",
      undefined,
      recordingFetch([], { "/api/v1/admin/queue/health": 401 })
    )
    const upstreamResult = await fetchDashboardData(
      request,
      "operator-secret",
      undefined,
      recordingFetch([], { "/api/v1/admin/queue/health": 500 })
    )

    expect(authResult.queue).toEqual({
      ok: false,
      error: "Dashboard session was rejected",
    })
    expect(upstreamResult.queue).toEqual({
      ok: false,
      error: "FinDB API request failed (500)",
    })
    expect(JSON.stringify([authResult, upstreamResult])).not.toContain(
      "sensitive upstream body"
    )
    expect(JSON.stringify([authResult, upstreamResult])).not.toContain(
      "operator-secret"
    )
  })

  it("rejects malformed endpoint responses without exposing their body", async () => {
    const result = await fetchDashboardData(
      request,
      "operator-secret",
      undefined,
      recordingFetch([], {}, "/api/v1/admin/queue/health")
    )

    expect(result.queue).toEqual({
      ok: false,
      error: "FinDB API returned an unexpected response",
    })
    expect(JSON.stringify(result)).not.toContain("sensitive response value")
  })

  it("patches a scheduler with the expected revision and no-store auth", async () => {
    const calls: RecordedCall[] = []
    const scheduler = responses["/api/v1/admin/schedulers"].data[0]
    const result = await patchSchedulerData(
      {
        schedulerKey: scheduler.scheduler_key,
        desiredState: "stopped",
        expectedRevision: scheduler.revision,
      },
      "owner-secret",
      "https://findb.internal:8443",
      async (input, init) => {
        calls.push({
          url: new URL(input instanceof Request ? input.url : input.toString()),
          init,
        })
        return Response.json({ success: true, data: scheduler })
      }
    )

    expect(result.data.scheduler_key).toBe(scheduler.scheduler_key)
    expect(calls).toHaveLength(1)
    expect(calls[0]?.url.pathname).toBe(
      `/api/v1/admin/schedulers/${scheduler.scheduler_key}`
    )
    expect(calls[0]?.init?.method).toBe("PATCH")
    expect(calls[0]?.init?.cache).toBe("no-store")
    expect(new Headers(calls[0]?.init?.headers).get("Authorization")).toBe(
      "Bearer owner-secret"
    )
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
      desired_state: "stopped",
      expected_revision: scheduler.revision,
    })
  })

  it("maps scheduler revision conflicts and auth failures without leaking upstream bodies", async () => {
    await expect(
      patchSchedulerData(
        {
          schedulerKey: "finlab_tw_1430_tw_equity_eod",
          desiredState: "running",
          expectedRevision: 4,
        },
        "owner-secret",
        undefined,
        async () => new Response("sensitive conflict body", { status: 409 })
      )
    ).rejects.toThrow("Scheduler revision is stale; refresh and retry")

    await expect(
      patchSchedulerData(
        {
          schedulerKey: "finlab_tw_1430_tw_equity_eod",
          desiredState: "running",
          expectedRevision: 4,
        },
        "owner-secret",
        undefined,
        async () => new Response("sensitive auth body", { status: 403 })
      )
    ).rejects.toThrow("Dashboard session was rejected")
  })
})
