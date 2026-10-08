// This loopback-only backend is launched exclusively by Playwright. The app
// still uses its normal login, session validation, server functions and CAS.
import { createServer } from "node:http"
import {
  marketFreshnessSchema,
  queueHealthSchema,
  schedulerSchema,
  type Scheduler,
} from "../../src/lib/admin-api.ts"
import { adminUserSchema } from "../../src/lib/admin-governance-api.ts"

const timestamp = "2026-10-07T01:00:00Z"
const user = adminUserSchema.parse({
  user_id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
  username: "owner",
  display_name: "E2E Owner",
  role: "owner",
  is_active: true,
  must_change_password: false,
})

function scheduler(key: string, provider: string, running = false) {
  return schedulerSchema.parse({
    scheduler_key: key,
    provider,
    dataset_keys: ["tw_equity_eod", "tw_etf_eod"],
    slot_id: "taiwan_market_window",
    scheduled_local_time: "14:30:00",
    timezone: "Asia/Taipei",
    desired_state: running ? "running" : "stopped",
    observed_state: running ? "running" : "stopped",
    revision: 1,
    last_heartbeat_at: timestamp,
    last_cycle_started_at: timestamp,
    last_cycle_completed_at: timestamp,
    last_error: null,
    created_at: timestamp,
    updated_at: timestamp,
    heartbeat_age_seconds: 5,
    start_allowed: true,
    full_market_enabled: true,
    environment: "production",
    admitted_dataset_keys: ["tw_equity_eod"],
    first_start_dates: { tw_equity_eod: "2026-10-06" },
    feed_readiness: [
      {
        dataset_key: "tw_equity_eod",
        ready: true,
        blockers: [],
        first_start_date: null,
      },
      {
        dataset_key: "tw_etf_eod",
        ready: false,
        blockers: ["baseline_missing"],
        first_start_date: null,
      },
    ],
  })
}

let rows: Scheduler[] = []
let mutations: unknown[] = []
let role = "owner"
let reads: Record<string, number> = {}
let plansError = false
let plansDelay = 0
const plans = Array.from({ length: 31 }, (_, index) => ({
  plan_id: `plan-${index}`,
  dataset_key: index === 30 ? "old_dataset" : "tw_futures_eod",
  provider: "taifex",
  trade_date: `2026-09-${String(30 - Math.min(index, 29)).padStart(2, "0")}`,
  release_id: `release-${index}`,
  deadline_at: timestamp,
  status: "incomplete",
  parts: [],
  summary: {
    expected: 5,
    data: 2,
    no_data: 1,
    missing: 1,
    blocked: 1,
    deadline_at: timestamp,
    is_late: true,
    gaps: [
      { member_key: "TX", status: "missing", reason: "pending" },
      { member_key: "MTX", status: "blocked", reason: "quota" },
    ],
  },
}))
function paginated<T>(items: T[], params: URLSearchParams) {
  const page = Number(params.get("page") ?? 1),
    size = Number(params.get("page_size") ?? 25)
  return {
    data: items.slice((page - 1) * size, page * size),
    pagination: {
      page,
      page_size: size,
      total_records: items.length,
      total_pages: Math.ceil(items.length / size),
    },
  }
}
const queue = queueHealthSchema.parse({
  counts: {},
  oldest_queued_at: null,
  oldest_queued_age_seconds: null,
  unpublished_outbox: 0,
  oldest_unpublished_outbox_at: null,
  oldest_unpublished_outbox_age_seconds: null,
  expired_leases: 0,
  retry_exhausted: 0,
  last_worker_heartbeat_at: timestamp,
  worker_heartbeat_age_seconds: 5,
  missing_deliveries: 0,
  oldest_missing_delivery_at: null,
  oldest_missing_delivery_age_seconds: null,
})

createServer(async (request, response) => {
  const url = new URL(request.url!, "http://127.0.0.1")
  const path = url.pathname
  let body = ""
  for await (const chunk of request) body += String(chunk)
  const data = body ? JSON.parse(body) : {}
  function send(payload: unknown, status = 200) {
    response.writeHead(status, { "Content-Type": "application/json" })
    response.end(JSON.stringify(payload))
  }
  if (path === "/health") return send({ ok: true })
  // Fixture controls exist on this test process only, never on the Dashboard.
  if (path === "/fixture/reset") {
    role = data.role ?? "owner"
    mutations = []
    reads = {}
    plansError = false
    plansDelay = 0
    rows = [
      scheduler("pilot_finlab", "finlab"),
      scheduler("pilot_shioaji", "shioaji", true),
      scheduler("full_market_finlab_v1", "finlab", true),
      scheduler("full_market_shioaji_v1", "shioaji"),
      {
        ...scheduler("full_market_taifex_v1", "taifex"),
        start_allowed: false,
        start_blockers: ["full_market_baseline_missing"],
      },
    ]
    if (data.allBlocked)
      rows = rows.map(row => ({
        ...row,
        desired_state: "stopped",
        observed_state: "stopped",
        start_allowed: false,
        start_blockers: ["full_market_baseline_missing"],
      }))
    return send({ ok: true })
  }
  if (path === "/fixture/state") return send({ rows, mutations, reads })
  if (path === "/fixture/delivery-config") {
    plansError = data.error ?? false
    plansDelay = data.delay ?? 0
    return send({ ok: true })
  }
  if (path === "/api/v1/admin/auth/login") {
    if (data.username !== role || data.password !== "fixture-password")
      return send({ detail: "Invalid credentials" }, 401)
    return send({
      access_token: `fixture-${role}`,
      token_type: "bearer",
      expires_at: "2099-01-01T00:00:00Z",
      user: { ...user, username: role, role },
    })
  }
  if (request.headers.authorization !== `Bearer fixture-${role}`)
    return send({ detail: "Unauthorized" }, 401)
  if (path === "/api/v1/admin/auth/me")
    return send({ ...user, username: role, role })
  reads[path] = (reads[path] ?? 0) + 1
  if (path === "/api/v1/admin/delivery-plans/datasets")
    return send({ data: ["old_dataset", "tw_futures_eod"] })
  if (path === "/api/v1/admin/delivery-plans") {
    if (plansDelay)
      await new Promise(resolve => setTimeout(resolve, plansDelay))
    if (plansError) return send({ detail: "Unavailable" }, 503)
    return send(
      paginated(
        plans.filter(
          plan =>
            (!url.searchParams.get("dataset_key") ||
              plan.dataset_key === url.searchParams.get("dataset_key")) &&
            (!url.searchParams.get("trade_date") ||
              plan.trade_date === url.searchParams.get("trade_date"))
        ),
        url.searchParams
      )
    )
  }
  if (path === "/api/v1/admin/missing-deliveries")
    return send(
      paginated(
        [
          {
            alert_id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
            dataset_key: "tw_equity_minute",
            source: "shioaji",
            schema_id: "market_minute",
            schema_version: 1,
            expected_data_date: "2026-10-07",
            status: "open",
            first_detected_at: timestamp,
            last_detected_at: timestamp,
            resolved_at: null,
          },
        ],
        url.searchParams
      )
    )
  if (path === "/api/v1/admin/historical-backfills")
    return send(paginated([], url.searchParams))
  if (path === "/api/v1/admin/historical-backfills/scopes")
    return send({
      data: [
        {
          provider: "shioaji",
          dataset_key: "tw_equity_minute",
          market: "TW",
          executable: true,
        },
      ],
    })
  if (path === "/api/v1/admin/schedulers")
    return send({ success: true, data: rows })
  if (path === "/api/v1/admin/market-freshness")
    return send(marketFreshnessSchema.parse({ success: true, data: [] }))
  if (path === "/api/v1/admin/queue/health") return send(queue)
  if (
    request.method === "PATCH" &&
    path.startsWith("/api/v1/admin/schedulers/")
  ) {
    if (role !== "owner") return send({ detail: "Forbidden" }, 403)
    const key = decodeURIComponent(path.split("/").at(-1)!)
    const row = rows.find(row => row.scheduler_key === key)
    if (!row) return send({ detail: "Not found" }, 404)
    if (row.revision !== data.expected_revision)
      return send({ detail: "Stale revision" }, 409)
    if (data.desired_state === "running" && !row.start_allowed)
      return send({ detail: { code: "scheduler_start_blocked" } }, 409)
    mutations.push({ scheduler_key: key, ...data })
    row.desired_state = data.desired_state
    row.revision += 1
    return send({ success: true, data: row })
  }
  return send({ detail: `Unexpected fixture request: ${path}` }, 404)
}).listen(18081, "127.0.0.1")
