import { describe, expect, it, vi } from "vitest"

import { mergeDashboardRefresh, type DashboardRequest } from "./admin-api"
import { fetchDashboardData, patchSchedulerData } from "./admin.server"

const timestamp = "2026-07-23T02:00:00Z"
const pagination = {
  page: 1,
  page_size: 10,
  total_records: 0,
  total_pages: 0,
}
const audit = {
  datasetKey: "tw_equity_eod",
  runId: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
  dateFrom: "2026-07-01",
  dateTo: "2026-07-23",
  page: 3,
  pageSize: 100,
}

const responses = {
  "/api/v1/admin/market-freshness": {
    success: true,
    data: [
      {
        market: "TW",
        scheduler_key: "finlab_tw_equity_eod_v1",
        provider: "finlab",
        dataset_keys: ["tw_equity_eod"],
        slot_id: "taiwan_market_window",
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
        slot_id: "western_markets_window",
        scheduled_local_time: "06:30:00",
        timezone: "Asia/Taipei",
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
    ],
  },
  "/api/v1/admin/missing-deliveries": {
    data: [],
    pagination,
  },
  "/api/v1/admin/historical-backfills": {
    data: [],
    pagination,
  },
  "/api/v1/admin/historical-backfills/scopes": { data: [] },
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
        raw_payload_id: "019565d2-f838-7c91-85c1-72d4d7bbbe99",
        idempotency_key: "delivery-1",
        run_id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
        dataset_key: "tw_equity_eod",
        source: "finlab",
        schema_id: null,
        schema_version: null,
        request_key: "request-1",
        payload: null,
        fetched_at: timestamp,
        expire_at: timestamp,
        created_at: timestamp,
      },
    ],
    pagination: { ...pagination, total_records: 1, total_pages: 1 },
  },
} as const

type RecordedCall = { url: URL; init: RequestInit | undefined }
type ResponsePath = keyof typeof responses

function request(view: DashboardRequest["view"]): DashboardRequest {
  return { view, audit: { ...audit } }
}

function responseFor(pathname: string) {
  const response = responses[pathname as ResponsePath]
  return new Response(JSON.stringify(response), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })
}

function recordingFetch(
  calls: RecordedCall[],
  statusByPath: Partial<Record<ResponsePath, number>> = {},
  malformedPath?: ResponsePath,
  timeoutPath?: ResponsePath
): typeof fetch {
  return async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : input.toString())
    calls.push({ url, init })
    if (url.pathname === timeoutPath) {
      throw new DOMException("The operation timed out", "TimeoutError")
    }
    const status = statusByPath[url.pathname as ResponsePath]
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
  it("viewer does not request historical backfill scope or status", async () => {
    const calls: RecordedCall[] = []
    const result = await fetchDashboardData(
      request("deliveries"),
      "viewer-secret",
      "https://findb.internal:8443",
      recordingFetch(calls),
      "viewer"
    )

    expect(calls.map(call => `${call.url.pathname}${call.url.search}`)).toEqual(
      ["/api/v1/admin/missing-deliveries?status=open&page=3&page_size=100"]
    )
    expect(result.view).toBe("deliveries")
    if (result.view === "deliveries") {
      expect(result.backfills.ok).toBe(false)
      expect(result.backfillScopes.ok).toBe(false)
    }
  })

  it.each([
    [
      "overview",
      [
        "/api/v1/admin/market-freshness",
        "/api/v1/admin/queue/health",
        "/api/v1/admin/schedulers",
      ],
    ],
    [
      "deliveries",
      [
        "/api/v1/admin/missing-deliveries?status=open&page=3&page_size=100",
        "/api/v1/admin/historical-backfills?page=1&page_size=25",
        "/api/v1/admin/historical-backfills/scopes",
      ],
    ],
    ["quality", ["/api/v1/admin/dq-issues"]],
    ["corrections", ["/api/v1/admin/corrections?page=3&page_size=100"]],
    ["rawPayloads", ["/api/v1/admin/raw-payloads"]],
  ] as const)(
    "%s calls only its expected endpoint set",
    async (view, expected) => {
      const calls: RecordedCall[] = []
      const result = await fetchDashboardData(
        request(view),
        "operator-secret",
        "https://findb.internal:8443",
        recordingFetch(calls)
      )

      expect(calls).toHaveLength(
        view === "overview" ? 3 : view === "deliveries" ? 3 : 1
      )
      expect(
        calls.map(
          call =>
            `${call.url.pathname}${view === "deliveries" ? call.url.search : view === "corrections" ? call.url.search : ""}`
        )
      ).toEqual(expected)
      expect(result.view).toBe(view)
      for (const call of calls) {
        expect(call.url.origin).toBe("https://findb.internal:8443")
        expect(call.init?.method).toBe("GET")
        expect(call.init?.cache).toBe("no-store")
        expect(new Headers(call.init?.headers).get("Authorization")).toBe(
          "Bearer operator-secret"
        )
        expect(call.init?.signal).toBeInstanceOf(AbortSignal)
      }
    }
  )

  it("includes include_payload=false while preserving every raw audit query field", async () => {
    const calls: RecordedCall[] = []
    const result = await fetchDashboardData(
      request("rawPayloads"),
      "operator-secret",
      undefined,
      recordingFetch(calls)
    )

    const url = calls[0]?.url
    expect(url?.pathname).toBe("/api/v1/admin/raw-payloads")
    expect(url?.searchParams.get("include_payload")).toBe("false")
    expect(url?.searchParams.get("dataset_key")).toBe(audit.datasetKey)
    expect(url?.searchParams.get("run_id")).toBe(audit.runId)
    expect(url?.searchParams.get("date_from")).toBe(audit.dateFrom)
    expect(url?.searchParams.get("date_to")).toBe(audit.dateTo)
    expect(url?.searchParams.get("page")).toBe("3")
    expect(url?.searchParams.get("page_size")).toBe("100")
    expect(result.view).toBe("rawPayloads")
  })

  it("supplies a 10-second AbortSignal and sanitizes a timeout into a degraded panel", async () => {
    const calls: RecordedCall[] = []
    const controller = new AbortController()
    const timeoutSpy = vi
      .spyOn(AbortSignal, "timeout")
      .mockImplementation(milliseconds => {
        expect(milliseconds).toBe(10_000)
        return controller.signal
      })

    try {
      const result = await fetchDashboardData(
        request("overview"),
        "operator-secret",
        undefined,
        recordingFetch(calls, {}, undefined, "/api/v1/admin/queue/health")
      )

      expect(timeoutSpy).toHaveBeenCalledTimes(3)
      expect(calls.every(call => call.init?.signal === controller.signal)).toBe(
        true
      )
      expect(result.view).toBe("overview")
      if (result.view === "overview") {
        expect(result.freshness.ok).toBe(true)
        expect(result.schedulers.ok).toBe(true)
        expect(result.queue).toEqual({
          ok: false,
          error: "FinDB API request timed out",
        })
      }
      expect(JSON.stringify(result)).not.toContain("The operation timed out")
    } finally {
      timeoutSpy.mockRestore()
    }
  })

  it("recognizes timeout errors across runtime realms by their standard name", async () => {
    const result = await fetchDashboardData(
      request("quality"),
      "operator-secret",
      undefined,
      async () => {
        const timeout = new Error("cross-realm timeout detail")
        timeout.name = "TimeoutError"
        throw timeout
      }
    )

    expect(result.view).toBe("quality")
    if (result.view === "quality") {
      expect(result.issues).toEqual({
        ok: false,
        error: "FinDB API request timed out",
      })
    }
    expect(JSON.stringify(result)).not.toContain("cross-realm timeout detail")
  })

  it("keeps successful panels when one endpoint fails", async () => {
    const result = await fetchDashboardData(
      request("quality"),
      "operator-secret",
      undefined,
      recordingFetch([], { "/api/v1/admin/dq-issues": 503 })
    )

    expect(result.view).toBe("quality")
    if (result.view === "quality") {
      expect(result.issues).toEqual({
        ok: false,
        error: "FinDB API request failed (503)",
      })
    }
  })

  it("retains the last successful panel when a refresh fails and reports the error", async () => {
    const current = await fetchDashboardData(
      request("overview"),
      "operator-secret",
      undefined,
      recordingFetch([])
    )
    const next = await fetchDashboardData(
      request("overview"),
      "operator-secret",
      undefined,
      recordingFetch([], { "/api/v1/admin/queue/health": 503 })
    )

    const merged = mergeDashboardRefresh(current, next)
    expect(merged.data.view).toBe("overview")
    if (
      merged.data.view === "overview" &&
      current.view === "overview" &&
      next.view === "overview"
    ) {
      expect(merged.data.freshness).toEqual(current.freshness)
      expect(merged.data.queue).toEqual(current.queue)
      expect(merged.data.schedulers).toEqual(next.schedulers)
    }
    expect(merged.errors).toEqual(["", "FinDB API request failed (503)", ""])
  })

  it("sanitizes authentication and upstream response bodies", async () => {
    await expect(
      fetchDashboardData(
        request("overview"),
        "operator-secret",
        undefined,
        recordingFetch([], { "/api/v1/admin/queue/health": 401 })
      )
    ).rejects.toThrow("Dashboard authentication required")

    const upstreamResult = await fetchDashboardData(
      request("overview"),
      "operator-secret",
      undefined,
      recordingFetch([], { "/api/v1/admin/queue/health": 500 })
    )

    expect(upstreamResult.view).toBe("overview")
    if (upstreamResult.view === "overview") {
      expect(upstreamResult.queue).toEqual({
        ok: false,
        error: "FinDB API request failed (500)",
      })
    }
    expect(JSON.stringify(upstreamResult)).not.toContain(
      "sensitive upstream body"
    )
    expect(JSON.stringify(upstreamResult)).not.toContain("operator-secret")
  })

  it("rejects malformed endpoint responses without exposing their body", async () => {
    const result = await fetchDashboardData(
      request("overview"),
      "operator-secret",
      undefined,
      recordingFetch([], {}, "/api/v1/admin/queue/health")
    )

    expect(result.view).toBe("overview")
    if (result.view === "overview") {
      expect(result.queue).toEqual({
        ok: false,
        error: "FinDB API returned an unexpected response",
      })
    }
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
          schedulerKey: "finlab_tw_equity_eod_v1",
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
          schedulerKey: "finlab_tw_equity_eod_v1",
          desiredState: "running",
          expectedRevision: 4,
        },
        "owner-secret",
        undefined,
        async () => new Response("sensitive auth body", { status: 403 })
      )
    ).rejects.toThrow("Dashboard authentication required")
  })
})
