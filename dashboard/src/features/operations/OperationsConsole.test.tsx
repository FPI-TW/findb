import "@testing-library/jest-dom/vitest"

import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { useState, type ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type {
  DashboardRequest,
  DashboardResponse,
  DQIssue,
  MissingDelivery,
  PanelResult,
  RawPayload,
  Scheduler,
  SchedulerMutationResponse,
  SchedulersResponse,
} from "../../lib/admin-api"
import { deliveriesSearchSchema } from "./operations.search"
import { marketFreshnessSchema } from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import { OperationsRefreshProvider } from "../../components/OperationsRefresh"
import { ProtectedQueryScopeProvider } from "../../components/ProtectedQueryScope"

const mocks = vi.hoisted(() => ({
  loadDashboard: vi.fn(),
  loadRawPayloadDetail: vi.fn(),
  loadDeliveryPlans: vi.fn(),
  loadDeliveryPlanDatasets: vi.fn(),
  loadUniverseCandidates: vi.fn(),
  loadUniverseDiff: vi.fn(),
  loadDeliveryResource: vi.fn(),
  updateScheduler: vi.fn(),
  createHistoricalBackfill: vi.fn(),
  cancelHistoricalBackfill: vi.fn(),
  previewHistoricalBackfill: vi.fn(),
  navigate: vi.fn(),
}))

vi.mock("@tanstack/react-router", () => ({
  // These component tests mount on the client; real hydration is covered in E2E.
  useHydrated: () => true,
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
  Outlet: () => null,
  useNavigate: () => mocks.navigate,
}))

vi.mock("@tanstack/react-start", () => ({
  useServerFn: (serverFn: unknown) => serverFn,
  createServerFn: () => {
    const builder = {
      validator: () => builder,
      handler: (handler: unknown) => handler,
    }
    return builder
  },
}))

vi.mock("../../lib/admin.functions", () => mocks)

import { DeliveriesPage } from "./operations.deliveries"
import {
  IngestionOverviewPanel,
  OperationsOverviewPage,
  describeSchedulerRuntime,
  schedulerActivityLabel,
  selectSchedulerRuntimeStatus,
} from "./operations.overview"
import { DQPolicyDetails } from "./operations.quality"
import {
  operationsKeys,
  updateOverviewSchedulerCache,
  useOperationsDashboardQuery,
  useRawPayloadDetailQuery,
} from "./operations.queries"

const timestamp = "2026-08-04T02:00:00Z"
const audit: DashboardRequest["audit"] = {
  datasetKey: "",
  runId: "",
  dateFrom: "",
  dateTo: "",
  page: 1,
  pageSize: 25,
}

function queryWrapper(queryClient: QueryClient, scope = "protected") {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <ProtectedQueryScopeProvider value={scope}>
        <OperationsRefreshProvider
          onAuthenticationFailure={() =>
            mocks.navigate({ to: "/login", replace: true })
          }
        >
          {children}
        </OperationsRefreshProvider>
      </ProtectedQueryScopeProvider>
    </QueryClientProvider>
  )
}

function qualityResponse(issueData: DQIssue[] = []): DashboardResponse {
  return {
    view: "quality",
    fetchedAt: timestamp,
    issues: {
      ok: true,
      data: {
        data: issueData,
        pagination: {
          page: 1,
          page_size: 25,
          total_records: issueData.length,
          total_pages: issueData.length ? 1 : 0,
        },
      },
    },
  }
}

function overviewResponse(schedulers: Scheduler[] = []): DashboardResponse {
  return {
    view: "overview",
    fetchedAt: timestamp,
    freshness: { ok: false, error: "not configured" },
    queue: { ok: false, error: "not configured" },
    schedulers: { ok: true, data: { success: true, data: schedulers } },
  }
}

function deliveriesResponse(missingDeliveries: MissingDelivery[] = []): Extract<
  DashboardResponse,
  { view: "deliveries" }
> & {
  deliveries: { ok: true }
  backfills: { ok: true }
  backfillScopes: { ok: true }
} {
  return {
    view: "deliveries",
    fetchedAt: timestamp,
    deliveries: {
      ok: true,
      data: {
        data: missingDeliveries,
        pagination: {
          page: 1,
          page_size: 25,
          total_records: missingDeliveries.length,
          total_pages: missingDeliveries.length ? 1 : 0,
        },
      },
    },
    backfills: {
      ok: true,
      data: {
        data: [],
        pagination: {
          page: 1,
          page_size: 25,
          total_records: 0,
          total_pages: 0,
        },
      },
    },
    backfillScopes: {
      ok: true,
      data: {
        data: [
          {
            provider: "taifex",
            dataset_key: "tw_futures_eod",
            market: "TW",
            executable: true,
          },
        ],
      },
    },
  }
}

function makeScheduler(overrides: Partial<Scheduler> = {}): Scheduler {
  return {
    scheduler_key: "scheduler-1",
    provider: "finlab",
    dataset_keys: ["tw_equity_eod"],
    slot_id: "taiwan_market_window",
    scheduled_local_time: "14:30:00",
    timezone: "Asia/Taipei",
    desired_state: "stopped",
    observed_state: "stopped",
    revision: 1,
    last_heartbeat_at: timestamp,
    last_cycle_started_at: timestamp,
    last_cycle_completed_at: timestamp,
    last_error: null,
    created_at: timestamp,
    updated_at: timestamp,
    heartbeat_age_seconds: 5,
    ...overrides,
  }
}

/** Scheduler rows rendered through the only production card surface. */
function SchedulerCards({
  result,
  role,
}: {
  result: PanelResult<SchedulersResponse>
  role: AdminRole
}) {
  return (
    <IngestionOverviewPanel
      freshnessResult={null}
      schedulersResult={result}
      loading={false}
      pending={false}
      freshnessError=""
      schedulersError=""
      role={role}
    />
  )
}

function makeFullMarketFreshness(
  provider = "taifex",
  overrides: Record<string, unknown> = {}
) {
  const scheduler = makeScheduler({
    scheduler_key: `full_market_${provider}_v1`,
    provider,
    last_heartbeat_at: null,
    heartbeat_age_seconds: null,
  })
  const result = marketFreshnessSchema.parse({
    success: true,
    data: [
      {
        ...scheduler,
        market: "TW",
        configuration_status: "ready",
        configuration_errors: [],
        monitor_kind: "full_market",
        activation_state: "not_activated",
        active_dataset_keys: [],
        pending_feeds: [
          {
            dataset_key: "tw_futures_eod",
            blockers: ["activation_disabled", "baseline_not_published"],
          },
        ],
        status: "not_due",
        expected_data_date: null,
        coverage_data_date: null,
        last_fetched_at: timestamp,
        last_successful_update_at: timestamp,
        last_complete_at: null,
        next_scheduled_at: timestamp,
        feed_count: 0,
        fresh_feed_count: 0,
        late_feed_count: 0,
        feeds: [],
        ...overrides,
      },
    ],
  }).data[0]
  if (!result) throw new Error("Missing full-market test fixture")
  return result
}

function makeMissingDelivery(
  overrides: Partial<MissingDelivery> = {}
): MissingDelivery {
  return {
    alert_id: "019565d2-f838-7c91-85c1-72d4d7bbbe90",
    dataset_key: "tw_futures_eod",
    source: "taifex",
    schema_id: "futures_eod",
    schema_version: 1,
    expected_data_date: "2026-08-31",
    status: "open",
    first_detected_at: timestamp,
    last_detected_at: timestamp,
    resolved_at: null,
    ...overrides,
  }
}

function makeRawPayload(): RawPayload {
  return {
    raw_payload_id: "019565d2-f838-7c91-85c1-72d4d7bbbe99",
    idempotency_key: "delivery-1",
    run_id: "019565d2-f838-7c91-85c1-72d4d7bbbe98",
    dataset_key: "tw_equity_eod",
    source: "finlab",
    schema_id: "market_eod",
    schema_version: 1,
    request_key: "request-1",
    payload: { rows: [{ symbol: "2330", close: 1000 }] },
    fetched_at: timestamp,
    expire_at: timestamp,
    created_at: timestamp,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.loadDashboard.mockResolvedValue(qualityResponse())
  mocks.loadRawPayloadDetail.mockResolvedValue(makeRawPayload())
  mocks.loadDeliveryPlans.mockResolvedValue({
    data: [],
    pagination: { page: 1, page_size: 25, total_records: 0, total_pages: 0 },
  })
  mocks.loadDeliveryPlanDatasets.mockResolvedValue({ data: [] })
  mocks.loadDeliveryResource.mockImplementation(async ({ data }) => {
    const response = await mocks.loadDashboard({
      data: {
        view: "deliveries",
        audit: { page: data.page, pageSize: data.pageSize },
      },
    })
    const result =
      response[
        data.resource === "alerts"
          ? "deliveries"
          : data.resource === "backfills"
            ? "backfills"
            : "backfillScopes"
      ]
    if (!result?.ok) throw new Error(result?.error ?? "Unavailable")
    return { resource: data.resource, result: result.data }
  })
  mocks.previewHistoricalBackfill.mockResolvedValue({
    provider: "taifex",
    dataset_key: "tw_futures_eod",
    market: "TW",
    scope_valid: true,
    scope_reason: null,
    days: [],
  })
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
})

describe("Operations query hooks", () => {
  it("retains source success timestamps and empty snapshots across partial failures", async () => {
    const client = new QueryClient()
    const initial = overviewResponse()
    mocks.loadDashboard.mockResolvedValue(initial)
    const hook = renderHook(
      () => useOperationsDashboardQuery("overview", audit),
      { wrapper: queryWrapper(client) }
    )
    await waitFor(() => expect(hook.result.current.data).toBeDefined())
    const original = hook.result.current.data!
    const failed = {
      ...initial,
      schedulers: { ok: false as const, error: "scheduler offline" },
    }
    mocks.loadDashboard.mockResolvedValue(failed)
    await act(() => hook.result.current.refetch())
    await waitFor(() =>
      expect(hook.result.current.data?.refreshErrors.schedulers).toBe(
        "scheduler offline"
      )
    )
    expect(original.sourceUpdatedAt.schedulers).toBeGreaterThan(0)
    expect(hook.result.current.data?.sourceUpdatedAt.schedulers).toBe(
      original.sourceUpdatedAt.schedulers
    )
    const next = hook.result.current.data
    expect(next?.view === "overview" && next.schedulers).toEqual(
      initial.view === "overview" && initial.schedulers
    )
    expect(next?.sourceUpdatedAt.freshness).toBe(0)
  })

  it("isolates protected query caches by authenticated session scope", async () => {
    const queryClient = new QueryClient()
    const first = renderHook(
      () => useOperationsDashboardQuery("quality", audit),
      { wrapper: queryWrapper(queryClient, "alice:owner") }
    )
    await waitFor(() => expect(first.result.current.data).toBeDefined())
    first.unmount()

    const second = renderHook(
      () => useOperationsDashboardQuery("quality", audit),
      { wrapper: queryWrapper(queryClient, "bob:viewer") }
    )
    await waitFor(() => expect(second.result.current.data).toBeDefined())

    expect(mocks.loadDashboard).toHaveBeenCalledTimes(2)
    expect(first.result.current.queryKey).not.toEqual(
      second.result.current.queryKey
    )
  })

  it("deduplicates same-key requests and isolates late responses by key", async () => {
    let resolveFirst!: (response: DashboardResponse) => void
    const first = new Promise<DashboardResponse>(resolve => {
      resolveFirst = resolve
    })
    mocks.loadDashboard.mockImplementation(
      ({ data }: { data: DashboardRequest }) =>
        data.audit.page === 1 ? first : Promise.resolve(qualityResponse())
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const { result, rerender } = renderHook(
      ({ page }: { page: number }) =>
        useOperationsDashboardQuery("quality", { ...audit, page }),
      { initialProps: { page: 1 }, wrapper: queryWrapper(queryClient) }
    )
    expect(mocks.loadDashboard).toHaveBeenCalledTimes(1)
    rerender({ page: 2 })
    await waitFor(() => expect(result.current.data?.view).toBe("quality"))
    expect(mocks.loadDashboard).toHaveBeenCalledTimes(2)
    await act(async () => {
      resolveFirst(qualityResponse())
      await first
    })
    expect(result.current.queryKey).toEqual(
      operationsKeys.dashboard("quality", { ...audit, page: 2 })
    )
  })

  it("polls only the overview, delivery, and quality keys", async () => {
    vi.useFakeTimers()
    const queryClient = new QueryClient()
    const { unmount } = renderHook(
      () => useOperationsDashboardQuery("quality", audit),
      { wrapper: queryWrapper(queryClient) }
    )
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(mocks.loadDashboard).toHaveBeenCalledTimes(2)
    unmount()

    mocks.loadDashboard.mockClear()
    const raw = renderHook(
      () => useOperationsDashboardQuery("rawPayloads", audit),
      { wrapper: queryWrapper(new QueryClient()) }
    )
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(mocks.loadDashboard).toHaveBeenCalledTimes(1)
    raw.unmount()
  })

  it("redirects on an authentication failure", async () => {
    mocks.loadDashboard.mockRejectedValue(
      new Error("Dashboard authentication required")
    )
    const queryClient = new QueryClient()
    renderHook(() => useOperationsDashboardQuery("quality", audit), {
      wrapper: queryWrapper(queryClient),
    })
    await waitFor(() =>
      expect(mocks.navigate).toHaveBeenCalledWith({
        to: "/login",
        replace: true,
      })
    )
  })

  it("lazy-loads raw detail and includes the complete search scope in its key", async () => {
    const scope = {
      dataset: "tw_equity_eod",
      run: "",
      from: "2026-08-01",
      to: "2026-08-04",
      p: 2,
      ps: 50,
    } as const
    const queryClient = new QueryClient()
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useRawPayloadDetailQuery(
          scope,
          makeRawPayload().raw_payload_id,
          enabled
        ),
      { initialProps: { enabled: false }, wrapper: queryWrapper(queryClient) }
    )
    expect(mocks.loadRawPayloadDetail).not.toHaveBeenCalled()
    rerender({ enabled: true })
    await waitFor(() => expect(result.current.data?.payload).toBeTruthy())
    expect(mocks.loadRawPayloadDetail).toHaveBeenCalledWith({
      data: { rawPayloadId: makeRawPayload().raw_payload_id },
    })
    expect(result.current.queryKey).toEqual(
      operationsKeys.rawPayloadDetail(scope, makeRawPayload().raw_payload_id)
    )
  })
})

describe("Operations presentation", () => {
  it("groups controls by market and renders published per-feed clocks without slots", async () => {
    const feed = (
      key: string,
      market: string,
      clock: string,
      zone: string
    ) => ({
      provider: market === "US" ? "twelve_data" : "finlab",
      dataset_key: key,
      market,
      scheduled_time: clock,
      source_timezone: zone,
      target_date_lag_days: 0,
      next_scheduled_at: timestamp,
      completion_deadline: {
        timezone: "Asia/Taipei",
        local_time: "23:00",
        trade_date_day_offset: 0,
      },
    })
    mocks.loadDashboard.mockResolvedValue(
      overviewResponse([
        makeScheduler({
          scheduler_key: "full_market_finlab_v1",
          dataset_keys: ["tw_equity_eod", "tw_etf_eod"],
          feed_schedules: [
            feed("tw_equity_eod", "TW", "18:00", "Asia/Taipei"),
            feed("tw_etf_eod", "TW", "18:00", "Asia/Taipei"),
          ],
        }),
        makeScheduler({
          scheduler_key: "full_market_twelve_data_v1",
          provider: "twelve_data",
          dataset_keys: ["us_equity_eod"],
          feed_schedules: [
            feed("us_equity_eod", "US", "17:00", "America/New_York"),
          ],
        }),
      ])
    )
    render(
      <QueryClientProvider client={new QueryClient()}>
        <OperationsOverviewPage role="viewer" />
      </QueryClientProvider>
    )
    expect(
      await screen.findByRole("heading", { name: "TW" })
    ).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "US" })).toBeInTheDocument()
    expect(
      screen.getByText(/us_equity_eod · 每日 17:00 · America\/New_York/)
    ).toBeInTheDocument()
    expect(
      screen.getByText(/tw_etf_eod · 每日 18:00 · Asia\/Taipei/)
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/Slot|taiwan_market_window|western_markets_window/)
    ).not.toBeInTheDocument()
    expect(screen.getAllByText("full_market_finlab_v1")).toHaveLength(1)
  })

  it("keeps the DQ policy detail bounded and hides nested raw payload values", () => {
    render(
      <DQPolicyDetails
        detail={{
          code: "close_gap",
          violations: [
            {
              code: "close_gap",
              observed: 101,
              raw_payload: "must-not-render",
            },
          ],
          violation_count: 1,
          truncated: true,
        }}
      />
    )
    expect(
      screen.getByText("1 個 policy violation（truncated／已截斷）")
    ).toBeInTheDocument()
    expect(
      screen.getByText(/code=close_gap · observed=101/)
    ).toBeInTheDocument()
    expect(screen.queryByText("must-not-render")).not.toBeInTheDocument()
  })

  describe("scheduler runtime status", () => {
    // Fixture heartbeats arrive at `timestamp` and are 5 seconds old.
    const beforeHeartbeat = (seconds: number) =>
      new Date(Date.parse(timestamp) - seconds * 1000).toISOString()
    const enabled = (overrides: Partial<Scheduler> = {}) =>
      makeScheduler({
        scheduler_key: "full_market_finlab_v1",
        desired_state: "running",
        observed_state: "stopped",
        acquisition_allowed: true,
        acquisition_blockers: [],
        expected_cycle_interval_seconds: 30,
        last_cycle_started_at: beforeHeartbeat(20),
        last_cycle_completed_at: beforeHeartbeat(19),
        ...overrides,
      })
    const summarize = (control: Scheduler) =>
      describeSchedulerRuntime({ control, freshness: null })

    it("treats observed=stopped between recent cycles as idle", () => {
      const runtime = summarize(enabled())
      expect(runtime.status).toBe("idle")
      expect(runtime.label).toBe("執行中：等待下一輪")
      expect(runtime.variant).toBe("default")
      expect(runtime.explanation).toMatch(/observed=stopped 是正常閒置/)
      expect(runtime.explanation).toMatch(/約每 30 秒開始下一輪/)
    })

    it("does not treat an authorization re-check between cycles as acquiring", () => {
      expect(summarize(enabled({ observed_state: "running" })).status).toBe(
        "idle"
      )
    })

    it("reports an in-progress cycle with its duration", () => {
      const runtime = summarize(
        enabled({
          observed_state: "running",
          last_cycle_started_at: beforeHeartbeat(295),
          last_cycle_completed_at: beforeHeartbeat(400),
        })
      )
      expect(runtime.status).toBe("acquiring")
      expect(runtime.label).toBe("抓取中")
      expect(runtime.explanation).toMatch(/^本輪 cycle 已執行 5 分鐘/)
    })

    it("explains a stop that waits for the current cycle", () => {
      const runtime = summarize(
        enabled({
          desired_state: "stopped",
          observed_state: "running",
          last_cycle_started_at: beforeHeartbeat(115),
          last_cycle_completed_at: beforeHeartbeat(200),
        })
      )
      expect(runtime.status).toBe("stopping")
      expect(runtime.label).toBe("停止中")
      expect(runtime.variant).toBe("warning")
      expect(runtime.explanation).toMatch(/本輪 cycle（已執行 2 分鐘）/)
    })

    it.each([
      ["an old cycle", beforeHeartbeat(600), beforeHeartbeat(599)],
      ["no cycle at all", null, null],
    ])("warns when an enabled control has %s", (_, started, completed) => {
      const runtime = summarize(
        enabled({
          last_cycle_started_at: started,
          last_cycle_completed_at: completed,
        })
      )
      expect(runtime.status).toBe("not_cycling")
      expect(runtime.label).toBe("已啟用但沒有進入 cycle")
      expect(runtime.variant).toBe("warning")
      expect(runtime.explanation).toMatch(/超過 90 秒沒有開始新的 cycle/)
      expect(runtime.blockers).toEqual([])
    })

    it("lists acquisition blockers for an enabled control", () => {
      const runtime = summarize(
        enabled({
          acquisition_allowed: false,
          acquisition_blockers: [
            "full_market_baseline_missing",
            "daily_acquisition_window_closed",
            "future_blocker",
          ],
        })
      )
      expect(runtime.status).toBe("acquisition_blocked")
      expect(runtime.label).toBe("已啟用但抓取被擋")
      expect(runtime.blockers).toEqual([
        "缺少已發布的官方 baseline",
        "目前不在每日抓取窗口內",
        "全市場設定或 scope 無效（future_blocker）",
      ])
      expect(
        summarize(
          enabled({ acquisition_allowed: false, acquisition_blockers: [] })
        ).explanation
      ).toMatch(/未提供原因/)
    })

    it("ignores the always-false authorization of a stopped control", () => {
      expect(
        summarize(
          enabled({
            desired_state: "stopped",
            acquisition_allowed: false,
            acquisition_blockers: ["full_market_baseline_missing"],
          })
        )
      ).toMatchObject({ status: "stopped", label: "已停止", blockers: [] })
    })

    it("flags heartbeats older than three poll intervals", () => {
      const stale = summarize(enabled({ heartbeat_age_seconds: 91 }))
      expect(stale.status).toBe("stale")
      expect(stale.label).toBe("心跳過期")
      expect(stale.explanation).toMatch(/超過 90 秒（3 × poll 間隔 30 秒）/)
      expect(
        summarize(
          enabled({
            heartbeat_age_seconds: 91,
            expected_cycle_interval_seconds: 60,
          })
        ).status
      ).toBe("idle")
      expect(
        summarize(
          enabled({
            heartbeat_age_seconds: 31,
            expected_cycle_interval_seconds: 10,
          })
        ).status
      ).toBe("stale")
    })

    it("falls back to a 30-second interval when the Backend omits it", () => {
      const legacy = enabled({ heartbeat_age_seconds: 91 })
      delete legacy.expected_cycle_interval_seconds
      expect(summarize(legacy).status).toBe("stale")
      expect(
        summarize(
          enabled({
            heartbeat_age_seconds: 89,
            expected_cycle_interval_seconds: null,
            last_cycle_started_at: timestamp,
            last_cycle_completed_at: timestamp,
          })
        ).status
      ).toBe("idle")
    })

    it("reports a runtime that never sent a heartbeat", () => {
      const runtime = summarize(
        enabled({ last_heartbeat_at: null, heartbeat_age_seconds: null })
      )
      expect(runtime.status).toBe("not_reported")
      expect(runtime.label).toBe("尚未回報")
      expect(runtime.explanation).toMatch(/尚未回報心跳/)
    })

    it("gives a fresh start one grace period before warning", () => {
      const old = {
        last_cycle_started_at: beforeHeartbeat(3600),
        last_cycle_completed_at: beforeHeartbeat(3599),
      }
      const starting = summarize(
        enabled({ ...old, running_since: beforeHeartbeat(10) })
      )
      expect(starting.status).toBe("starting")
      expect(starting.label).toBe("啟動中：等待第一輪 cycle")
      expect(starting.explanation).toMatch(/Owner 已於 15 秒前啟用/)
      expect(
        summarize(enabled({ ...old, running_since: beforeHeartbeat(200) }))
          .status
      ).toBe("not_cycling")
    })

    it("separates a cycle that started but did not complete", () => {
      const runtime = summarize(
        enabled({
          last_cycle_completed_at: beforeHeartbeat(100),
          last_error: "full_market_cycle_blocked",
        })
      )
      expect(runtime.status).toBe("cycle_failed")
      expect(runtime.explanation).toMatch(/（full_market_cycle_blocked）/)
    })

    it("counts enabled and acquiring controls separately", () => {
      expect(
        schedulerActivityLabel([
          { desired_state: "running", status: "acquiring" },
          { desired_state: "running", status: "idle" },
          { desired_state: "stopped", status: "stopping" },
        ])
      ).toBe("2 已啟用 · 1 抓取中")
    })

    it("shows Owner intent and runtime state as separate card sections", () => {
      render(
        <QueryClientProvider client={new QueryClient()}>
          <SchedulerCards
            result={{
              ok: true,
              data: {
                success: true,
                data: [
                  enabled({
                    observed_state: "running",
                    last_cycle_started_at: beforeHeartbeat(55),
                    last_cycle_completed_at: beforeHeartbeat(120),
                  }),
                  enabled({
                    scheduler_key: "full_market_taifex_v1",
                    provider: "taifex",
                    acquisition_allowed: false,
                    acquisition_blockers: ["full_market_baseline_missing"],
                  }),
                ],
              },
            }}
            role="viewer"
          />
        </QueryClientProvider>
      )
      const owner = screen.getByRole("region", {
        name: "full_market_finlab_v1 Owner 設定",
      })
      const runtime = screen.getByRole("region", {
        name: "full_market_finlab_v1 Runtime 實況",
      })
      expect(owner).toHaveTextContent("已啟用")
      expect(owner).toHaveTextContent("r1")
      expect(runtime).toHaveTextContent("抓取中")
      expect(runtime).toHaveTextContent("本輪 cycle 已執行 1 分鐘")
      expect(
        screen.getByRole("region", {
          name: "full_market_taifex_v1 Runtime 實況",
        })
      ).toHaveTextContent("缺少已發布的官方 baseline")
      expect(
        screen.getByText(/^2 已啟用 · 1 抓取中（共 2 個排程）$/)
      ).toBeInTheDocument()
      expect(screen.getByText("尚不可部署：2 個排程還沒停好")).toBeVisible()
      expect(
        screen.getByText(/Owner 設定 已啟用 · Runtime 實況 已啟用但抓取被擋/)
      ).toBeInTheDocument()
      expect(
        screen.getByText(
          "部署前人工確認：請停止所有排程，確認每張卡片的「Owner 設定」與「Runtime 實況」均為已停止。"
        )
      ).toBeInTheDocument()
      expect(screen.getAllByText("詳細")).toHaveLength(2)
    })

    it("passes the deployment check only when every control is stopped", () => {
      render(
        <QueryClientProvider client={new QueryClient()}>
          <SchedulerCards
            result={{
              ok: true,
              data: {
                success: true,
                data: [
                  makeScheduler({ scheduler_key: "full_market_finlab_v1" }),
                  makeScheduler({
                    scheduler_key: "full_market_taifex_v1",
                    provider: "taifex",
                  }),
                ],
              },
            }}
            role="viewer"
          />
        </QueryClientProvider>
      )
      expect(
        screen.getByText(
          /全部 2 個排程的「Owner 設定」與「Runtime 實況」均為已停止/
        )
      ).toBeInTheDocument()
      expect(
        screen.getByText(/^0 已啟用 · 0 抓取中（共 2 個排程）$/)
      ).toBeInTheDocument()
      expect(screen.queryByText(/尚不可部署/)).not.toBeInTheDocument()
    })

    it("shows current trade-date delivery progress from freshness", () => {
      const freshness = makeFullMarketFreshness("twelve_data", {
        activation_state: "activated",
        feeds: [
          {
            dataset_key: "us_equity_eod",
            source: "twelve_data",
            schema_id: "market_eod",
            schema_version: 1,
            expected_data_date: "2026-11-06",
            latest_successful_data_date: "2026-11-05",
            last_fetched_at: null,
            last_completed_at: null,
            last_run_id: null,
            total_records: 5550,
            success_records: 0,
            failed_records: 12,
            policy_outcome: null,
            open_missing_delivery_alert: false,
            last_failure_code: null,
            configuration_error: null,
            status: "late",
          },
          {
            dataset_key: "us_etf_eod",
            source: "twelve_data",
            schema_id: "market_eod",
            schema_version: 1,
            expected_data_date: "2026-11-06",
            latest_successful_data_date: null,
            last_fetched_at: null,
            last_completed_at: null,
            last_run_id: null,
            total_records: null,
            success_records: null,
            failed_records: null,
            policy_outcome: null,
            open_missing_delivery_alert: false,
            last_failure_code: null,
            configuration_error: null,
            status: "not_due",
          },
        ],
      })
      render(
        <QueryClientProvider client={new QueryClient()}>
          <IngestionOverviewPanel
            freshnessResult={{
              ok: true,
              data: { success: true, data: [freshness] },
            }}
            schedulersResult={null}
            loading={false}
            pending={false}
            freshnessError=""
            schedulersError=""
            role="viewer"
          />
        </QueryClientProvider>
      )
      expect(screen.getByText("當期交付進度")).toBeInTheDocument()
      const progress = screen.getByText("當期交付進度").nextElementSibling
      expect(progress).toHaveTextContent(
        "us_equity_eod · 2026-11-06：data 0 / 5,550 · blocked 12 · 延遲"
      )
      expect(progress).toHaveTextContent(
        "us_etf_eod · 2026-11-06：尚未建立交付計畫 · 尚未到期"
      )
    })
  })

  it.each(["factset", "finlab", "taifex", "twelve_data"])(
    "shows dormant %s full-market status without inventing heartbeat",
    provider => {
      const freshness = makeFullMarketFreshness(provider)
      expect(
        selectSchedulerRuntimeStatus({ control: freshness, freshness })
      ).toBe("not_activated")
      expect(freshness.last_heartbeat_at).toBeNull()
      expect(
        selectSchedulerRuntimeStatus({
          control: { ...freshness, desired_state: "running" },
          freshness,
        })
      ).toBe("configuration_error")
    }
  )

  it("separates explicit stop/deactivation from missing and stale active runtime", () => {
    const freshness = makeFullMarketFreshness("taifex", {
      activation_state: "activated",
      active_dataset_keys: ["tw_futures_eod"],
    })
    expect(
      selectSchedulerRuntimeStatus({ control: freshness, freshness })
    ).toBe("stopped")
    expect(
      selectSchedulerRuntimeStatus({
        control: {
          ...freshness,
          last_heartbeat_at: timestamp,
          heartbeat_age_seconds: 1000,
        },
        freshness,
      })
    ).toBe("stopped")
    expect(
      selectSchedulerRuntimeStatus({
        control: { ...freshness, desired_state: "running" },
        freshness,
      })
    ).toBe("not_reported")
    expect(
      selectSchedulerRuntimeStatus({
        control: {
          ...freshness,
          desired_state: "running",
          last_heartbeat_at: timestamp,
          heartbeat_age_seconds: 91,
        },
        freshness,
      })
    ).toBe("stale")
    expect(
      selectSchedulerRuntimeStatus({
        control: {
          ...freshness,
          observed_state: "running",
          last_heartbeat_at: timestamp,
          heartbeat_age_seconds: 5,
        },
        freshness,
      })
    ).toBe("stopping")
    expect(
      selectSchedulerRuntimeStatus({
        control: freshness,
        freshness: { ...freshness, activation_state: "deactivated" },
      })
    ).toBe("deactivated")
    expect(
      selectSchedulerRuntimeStatus({
        control: freshness,
        freshness: {
          ...freshness,
          configuration_status: "error",
          configuration_errors: ["malformed_governance"],
        },
      })
    ).toBe("configuration_error")
  })

  it("distinguishes pending activation from blockers", () => {
    const freshness = makeFullMarketFreshness("taifex", {
      scheduler_key: "full_market_taifex_v1",
      execution_profile: "full_market",
      pending_feeds: [
        { dataset_key: "tw_futures_eod", blockers: [] },
        {
          dataset_key: "tw_futures_daily",
          blockers: ["baseline_not_published"],
        },
      ],
    })
    render(
      <QueryClientProvider client={new QueryClient()}>
        <IngestionOverviewPanel
          freshnessResult={{
            ok: true,
            data: { success: true, data: [freshness] },
          }}
          schedulersResult={null}
          loading={false}
          pending={false}
          freshnessError=""
          schedulersError=""
          role="viewer"
        />
      </QueryClientProvider>
    )
    expect(screen.getByText("尚未啟用")).toBeInTheDocument()
    expect(screen.getByText("tw_futures_eod：尚未啟用")).toBeInTheDocument()
    expect(
      screen.getByText("tw_futures_daily：baseline_not_published")
    ).toBeInTheDocument()
    expect(screen.getByText("尚未納入抓取範圍：")).toBeInTheDocument()
    expect(screen.getByText(/^已啟用範圍：無/)).toBeInTheDocument()
    expect(
      screen.getByText(/資料更新不代表此模式已啟用或排程已回報心跳/)
    ).toBeInTheDocument()
    expect(screen.getByText(/^排程尚未回報心跳/)).toBeInTheDocument()
    expect(screen.queryByText(/Baseline／Calendar/)).not.toBeInTheDocument()
    expect(screen.queryByText(/全市場已啟用範圍/)).not.toBeInTheDocument()
    expect(screen.queryByText("執行中")).not.toBeInTheDocument()
  })

  it.each(["not_activated", "activated"])(
    "shows registry contract scope errors for %s full-market cards",
    activationState => {
      const freshness = makeFullMarketFreshness("finlab", {
        activation_state: activationState,
        configuration_status: "error",
        configuration_errors: ["tw_equity_eod:contract_scope_mismatch"],
        pending_feeds: [
          {
            dataset_key: "tw_etf_eod",
            blockers: ["activation_disabled", "dataset_inactive"],
          },
        ],
      })
      render(
        <QueryClientProvider client={new QueryClient()}>
          <IngestionOverviewPanel
            freshnessResult={{
              ok: true,
              data: { success: true, data: [freshness] },
            }}
            schedulersResult={null}
            loading={false}
            pending={false}
            freshnessError=""
            schedulersError=""
            role="viewer"
          />
        </QueryClientProvider>
      )
      expect(screen.getByText("設定錯誤")).toBeInTheDocument()
      expect(
        screen.getByText("tw_equity_eod:contract_scope_mismatch", {
          exact: false,
        })
      ).toBeInTheDocument()
      expect(
        screen.getByText("tw_etf_eod：activation_disabled；dataset_inactive")
      ).toBeInTheDocument()
      expect(screen.queryByText("尚未啟用")).not.toBeInTheDocument()
    }
  )

  it("updates the overview cache immediately and invalidates the same key after mutation", async () => {
    const queryClient = new QueryClient()
    const current = {
      ...overviewResponse([makeScheduler()]),
      refreshErrors: {},
      sourceUpdatedAt: {},
    }
    const key = operationsKeys.overview(audit)
    queryClient.setQueryData(key, current)
    const invalidation = vi.spyOn(queryClient, "invalidateQueries")
    const response: SchedulerMutationResponse = {
      success: true,
      data: makeScheduler({ desired_state: "running", revision: 2 }),
    }
    await updateOverviewSchedulerCache(queryClient, audit, response)
    const cached = queryClient.getQueryData<typeof current>(key)
    expect(cached?.view === "overview" ? cached.schedulers : undefined).toEqual(
      expect.objectContaining({ ok: true })
    )
    expect(invalidation).toHaveBeenCalledWith({ queryKey: key, exact: true })
  })

  it("does not flash a configuration error while a started scheduler refetches", async () => {
    const queryClient = new QueryClient()
    const freshness = makeFullMarketFreshness("finlab")
    const scheduler = makeScheduler({
      scheduler_key: freshness.scheduler_key,
    })
    const key = operationsKeys.overview(audit)
    queryClient.setQueryData(key, {
      view: "overview",
      fetchedAt: timestamp,
      freshness: { ok: true, data: { success: true, data: [freshness] } },
      queue: { ok: false, error: "not configured" },
      schedulers: { ok: true, data: { success: true, data: [scheduler] } },
      refreshErrors: {},
      sourceUpdatedAt: {},
    })
    vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue()
    await updateOverviewSchedulerCache(queryClient, audit, {
      success: true,
      data: makeScheduler({
        scheduler_key: freshness.scheduler_key,
        desired_state: "running",
        revision: 2,
      }),
    })
    const cached = queryClient.getQueryData<DashboardResponse>(key)
    if (
      cached?.view !== "overview" ||
      !cached.freshness.ok ||
      !cached.schedulers.ok
    ) {
      throw new Error("Missing overview cache")
    }
    const staleFreshness = cached.freshness.data.data[0]!
    const control = cached.schedulers.data.data[0]!
    // Freshness keeps the revision its activation_state was computed for.
    expect(staleFreshness).toMatchObject({
      revision: 1,
      activation_state: "not_activated",
    })
    expect(control).toMatchObject({ desired_state: "running", revision: 2 })
    expect(
      selectSchedulerRuntimeStatus({ control, freshness: staleFreshness })
    ).toBe("idle")
    render(
      <QueryClientProvider client={queryClient}>
        <IngestionOverviewPanel
          freshnessResult={cached.freshness}
          schedulersResult={cached.schedulers}
          loading={false}
          pending={false}
          freshnessError=""
          schedulersError=""
          role="owner"
        />
      </QueryClientProvider>
    )
    expect(screen.queryByText("設定錯誤")).not.toBeInTheDocument()

    const refetched = { ...freshness, desired_state: "running", revision: 2 }
    expect(
      selectSchedulerRuntimeStatus({
        control,
        freshness: { ...refetched, activation_state: "activated" },
      })
    ).toBe("idle")
    // At the same revision, a running scheduler without activation is real.
    expect(
      selectSchedulerRuntimeStatus({ control, freshness: refetched })
    ).toBe("configuration_error")
  })

  it("advances the freshness revision when freshness is the only control source", async () => {
    const queryClient = new QueryClient()
    const freshness = makeFullMarketFreshness("finlab")
    const key = operationsKeys.overview(audit)
    queryClient.setQueryData(key, {
      view: "overview",
      fetchedAt: timestamp,
      freshness: { ok: true, data: { success: true, data: [freshness] } },
      queue: { ok: false, error: "not configured" },
      schedulers: { ok: false, error: "unavailable" },
      refreshErrors: {},
      sourceUpdatedAt: {},
    })
    vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue()
    await updateOverviewSchedulerCache(queryClient, audit, {
      success: true,
      data: makeScheduler({
        scheduler_key: freshness.scheduler_key,
        desired_state: "running",
        revision: 2,
      }),
    })
    const cached = queryClient.getQueryData<DashboardResponse>(key)
    // The card's next mutation must send the current expected revision.
    expect(
      cached?.view === "overview" && cached.freshness.ok
        ? cached.freshness.data.data[0]?.revision
        : undefined
    ).toBe(2)
  })

  it("renders an explicit initial skeleton for the overview", () => {
    mocks.loadDashboard.mockReturnValue(new Promise(() => undefined))
    const queryClient = new QueryClient()
    render(
      <QueryClientProvider client={queryClient}>
        <OperationsOverviewPage role="viewer" />
      </QueryClientProvider>
    )
    expect(screen.getAllByRole("status").length).toBeGreaterThan(0)
  })

  it("renders revision-safe bulk controls on the owner overview route", async () => {
    const schedulers = [
      makeScheduler({
        scheduler_key: "scheduler-finlab",
        desired_state: "running",
        observed_state: "running",
        revision: 7,
      }),
      makeScheduler({
        scheduler_key: "scheduler-taifex",
        provider: "taifex",
        desired_state: "running",
        observed_state: "running",
        revision: 11,
      }),
    ]
    mocks.loadDashboard.mockResolvedValue(overviewResponse(schedulers))

    render(
      <QueryClientProvider client={new QueryClient()}>
        <OperationsOverviewPage role="owner" />
      </QueryClientProvider>
    )

    const stopAllButton = await screen.findByRole("button", {
      name: "全部停止",
    })
    expect(screen.getByRole("button", { name: "全部啟動" })).toBeDisabled()
    fireEvent.click(stopAllButton)
    expect(
      screen.getByRole("heading", { name: "確認全部停止排程？" })
    ).toBeInTheDocument()
    expect(
      screen.getByText("finlab / scheduler-finlab / r7")
    ).toBeInTheDocument()
    expect(
      screen.getByText("taifex / scheduler-taifex / r11")
    ).toBeInTheDocument()
  })

  it("keeps bulk controls hidden from viewers on the overview route", async () => {
    mocks.loadDashboard.mockResolvedValue(
      overviewResponse([makeScheduler({ scheduler_key: "scheduler-finlab" })])
    )

    render(
      <QueryClientProvider client={new QueryClient()}>
        <OperationsOverviewPage role="viewer" />
      </QueryClientProvider>
    )

    expect(await screen.findByText("scheduler-finlab")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "全部停止" })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "全部啟動" })
    ).not.toBeInTheDocument()
  })

  it("keeps scheduler controls owner-only", () => {
    const scheduler = makeScheduler()
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerCards
          result={{ ok: true, data: { success: true, data: [scheduler] } }}
          role="viewer"
        />
      </QueryClientProvider>
    )
    expect(
      screen.getByText("唯讀：只有 owner 可以變更排程狀態。")
    ).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "全部停止" })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "全部啟動" })
    ).not.toBeInTheDocument()
  })

  it("updates every scheduler sequentially with revision-safe bulk controls", async () => {
    const schedulers = [
      makeScheduler({
        scheduler_key: "scheduler-finlab",
        provider: "finlab",
        desired_state: "running",
        observed_state: "running",
        revision: 7,
      }),
      makeScheduler({
        scheduler_key: "scheduler-taifex",
        provider: "taifex",
        desired_state: "running",
        observed_state: "running",
        revision: 11,
      }),
    ]
    let resolveFirst!: (response: SchedulerMutationResponse) => void
    const firstUpdate = new Promise<SchedulerMutationResponse>(resolve => {
      resolveFirst = resolve
    })
    mocks.updateScheduler
      .mockReturnValueOnce(firstUpdate)
      .mockResolvedValueOnce({
        success: true,
        data: {
          ...schedulers[1]!,
          desired_state: "stopped",
          revision: 12,
        },
      })

    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerCards
          result={{ ok: true, data: { success: true, data: schedulers } }}
          role="owner"
        />
      </QueryClientProvider>
    )

    expect(screen.getByText(/部署前人工確認：/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "全部停止" }))
    expect(
      screen.getByRole("heading", { name: "確認全部停止排程？" })
    ).toBeInTheDocument()
    expect(
      screen.getByText("finlab / scheduler-finlab / r7")
    ).toBeInTheDocument()
    expect(
      screen.getByText("taifex / scheduler-taifex / r11")
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "確認全部停止" }))

    await waitFor(() => expect(mocks.updateScheduler).toHaveBeenCalledTimes(1))
    expect(screen.getByRole("button", { name: "全部停止" })).toBeDisabled()
    expect(
      screen.getByRole("button", {
        name: "taifex scheduler-taifex 設為已停止",
      })
    ).toBeDisabled()
    expect(mocks.updateScheduler).toHaveBeenNthCalledWith(1, {
      data: {
        schedulerKey: "scheduler-finlab",
        desiredState: "stopped",
        expectedRevision: 7,
      },
    })

    await act(async () => {
      resolveFirst({
        success: true,
        data: {
          ...schedulers[0]!,
          desired_state: "stopped",
          revision: 8,
        },
      })
      await firstUpdate
    })

    await waitFor(() => expect(mocks.updateScheduler).toHaveBeenCalledTimes(2))
    expect(mocks.updateScheduler).toHaveBeenNthCalledWith(2, {
      data: {
        schedulerKey: "scheduler-taifex",
        desiredState: "stopped",
        expectedRevision: 11,
      },
    })
  })

  it("starts only eligible schedulers and explains skipped full-market controls", async () => {
    const schedulers = [
      makeScheduler({
        scheduler_key: "full_market_finlab_v1",
        desired_state: "stopped",
        start_allowed: true,
        start_blockers: [],
      }),
      makeScheduler({
        scheduler_key: "full_market_twelve_data_v1",
        provider: "twelve_data",
        desired_state: "stopped",
        start_allowed: false,
        start_blockers: ["full_market_no_enabled_datasets"],
      }),
      makeScheduler({
        scheduler_key: "full_market_taifex_v1",
        desired_state: "stopped",
      }),
    ]
    mocks.updateScheduler.mockImplementation(async ({ data }) => ({
      success: true,
      data: {
        ...schedulers.find(row => row.scheduler_key === data.schedulerKey)!,
        desired_state: "running",
        revision: 5,
      },
    }))
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerCards
          result={{ ok: true, data: { success: true, data: schedulers } }}
          role="owner"
        />
      </QueryClientProvider>
    )
    expect(
      screen.getByText(/全部啟動將跳過 2 個 Scheduler/)
    ).toBeInTheDocument()
    expect(
      screen.getByRole("button", {
        name: "twelve_data full_market_twelve_data_v1 設為已啟用",
      })
    ).toBeDisabled()
    fireEvent.click(screen.getByRole("button", { name: "全部啟動" }))
    expect(screen.getByText(/將依序更新 1 個/)).toBeInTheDocument()
    expect(screen.getByText(/跳過 2 個無法啟動/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "確認全部啟動" }))
    await waitFor(() => expect(mocks.updateScheduler).toHaveBeenCalledTimes(1))
    expect(
      mocks.updateScheduler.mock.calls.map(call => call[0].data.schedulerKey)
    ).toEqual(["full_market_finlab_v1"])
  })

  it("explains FactSet HK start blockers in cards and bulk confirmation", () => {
    const schedulerKey = "full_market_factset_v1"
    const reason =
      "尚無已發布的 FactSet HKEX baseline；尚未提交 HK provider 切換"
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerCards
          result={{
            ok: true,
            data: {
              success: true,
              data: [
                makeScheduler({
                  scheduler_key: schedulerKey,
                  provider: "factset",
                  desired_state: "stopped",
                  start_allowed: false,
                  start_blockers: [
                    "full_market_baseline_missing",
                    "provider_acquisition_cutover",
                  ],
                }),
                makeScheduler({
                  scheduler_key: "full_market_finlab_v1",
                  provider: "finlab",
                  desired_state: "stopped",
                  start_allowed: true,
                }),
              ],
            },
          }}
          role="owner"
        />
      </QueryClientProvider>
    )
    expect(screen.getByText(`無法啟動：${reason}`)).toBeInTheDocument()
    expect(
      screen.getByText(
        `全部啟動將跳過 1 個 Scheduler：${schedulerKey}（${reason}）`
      )
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/全市場設定或 scope 無效/)
    ).not.toBeInTheDocument()
    expect(
      screen.getByRole("button", {
        name: `factset ${schedulerKey} 設為已啟用`,
      })
    ).toBeDisabled()
    fireEvent.click(screen.getByRole("button", { name: "全部啟動" }))
    expect(
      screen.getByText(
        `跳過 1 個無法啟動的 Scheduler：${schedulerKey}（${reason}）`
      )
    ).toBeInTheDocument()
    expect(mocks.updateScheduler).not.toHaveBeenCalled()
  })

  it("disables all-blocked starts while allowing a running invalid control to stop", () => {
    const schedulers = [
      makeScheduler({
        scheduler_key: "full_market_finlab_v1",
        provider: "finlab",
        desired_state: "stopped",
        start_allowed: false,
        start_blockers: ["full_market_configuration_invalid"],
      }),
      makeScheduler({
        scheduler_key: "full_market_taifex_v1",
        provider: "taifex",
        desired_state: "running",
        start_allowed: false,
        start_blockers: ["full_market_no_enabled_datasets"],
      }),
    ]
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerCards
          result={{ ok: true, data: { success: true, data: schedulers } }}
          role="owner"
        />
      </QueryClientProvider>
    )
    expect(screen.getByRole("button", { name: "全部啟動" })).toBeDisabled()
    expect(
      screen.getByRole("button", {
        name: "finlab full_market_finlab_v1 設為已啟用",
      })
    ).toBeDisabled()
    expect(
      screen.getByRole("button", {
        name: "taifex full_market_taifex_v1 設為已停止",
      })
    ).toBeEnabled()
    fireEvent.click(screen.getByRole("button", { name: "全部停止" }))
    expect(screen.getByText(/將依序更新 1 個/)).toBeInTheDocument()
    expect(
      screen.getByText(/taifex \/ full_market_taifex_v1/)
    ).toBeInTheDocument()
  })

  it("allows an eligible full-market single start and displays a changed-governance rejection", async () => {
    const scheduler = makeScheduler({
      scheduler_key: "full_market_finlab_v1",
      provider: "finlab",
      desired_state: "stopped",
      start_allowed: true,
    })
    mocks.updateScheduler.mockRejectedValue(
      new Error(
        "全市場啟動條件已變更或尚未完成，請重新整理並確認已發布 baseline 與完整日曆。"
      )
    )
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerCards
          result={{ ok: true, data: { success: true, data: [scheduler] } }}
          role="owner"
        />
      </QueryClientProvider>
    )
    fireEvent.click(
      screen.getByRole("button", {
        name: "finlab full_market_finlab_v1 設為已啟用",
      })
    )
    fireEvent.click(screen.getByRole("button", { name: "確認啟用" }))
    expect(await screen.findByText(/全市場啟動條件已變更/)).toBeInTheDocument()
    expect(
      screen.queryByText(/排程版本已被其他使用者更新/)
    ).not.toBeInTheDocument()
  })

  it("keeps freshness-only cards out of bulk writes", () => {
    const rows = [
      makeScheduler({
        scheduler_key: "full_market_twelve_data_v1",
        provider: "twelve_data",
        start_allowed: true,
      }),
    ]
    render(
      <QueryClientProvider client={new QueryClient()}>
        <IngestionOverviewPanel
          freshnessResult={{
            ok: true,
            data: {
              success: true,
              data: [makeFullMarketFreshness("finlab")],
            },
          }}
          schedulersResult={{ ok: true, data: { success: true, data: rows } }}
          loading={false}
          pending={false}
          freshnessError=""
          schedulersError=""
          role="owner"
        />
      </QueryClientProvider>
    )
    expect(screen.getByText("full_market_finlab_v1")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "全部啟動" }))
    expect(screen.getByText(/將依序更新 1 個/)).toBeInTheDocument()
    expect(
      screen.getByText("twelve_data / full_market_twelve_data_v1 / r1")
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/finlab \/ full_market_finlab_v1/)
    ).not.toBeInTheDocument()
    expect(mocks.updateScheduler).not.toHaveBeenCalled()
  })

  it("continues a bulk scheduler update after a revision conflict", async () => {
    const schedulers = [
      makeScheduler({
        scheduler_key: "scheduler-conflict",
        desired_state: "running",
        observed_state: "running",
        revision: 2,
      }),
      makeScheduler({
        scheduler_key: "scheduler-next",
        provider: "taifex",
        desired_state: "running",
        observed_state: "running",
        revision: 4,
      }),
    ]
    mocks.updateScheduler
      .mockRejectedValueOnce({ status: 409 })
      .mockResolvedValueOnce({
        success: true,
        data: {
          ...schedulers[1]!,
          desired_state: "stopped",
          revision: 5,
        },
      })

    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerCards
          result={{ ok: true, data: { success: true, data: schedulers } }}
          role="owner"
        />
      </QueryClientProvider>
    )

    fireEvent.click(screen.getByRole("button", { name: "全部停止" }))
    fireEvent.click(screen.getByRole("button", { name: "確認全部停止" }))

    await waitFor(() => expect(mocks.updateScheduler).toHaveBeenCalledTimes(2))
    expect(
      screen.getByText("排程版本已被其他使用者更新，請先重新整理後再試。")
    ).toBeInTheDocument()
  })

  it("shows closed dates as skipped and allows confirmation-based backfill creation", async () => {
    mocks.loadDashboard.mockResolvedValue(deliveriesResponse())
    mocks.previewHistoricalBackfill.mockResolvedValue({
      provider: "taifex",
      dataset_key: "tw_futures_eod",
      market: "TW",
      scope_valid: true,
      scope_reason: null,
      days: [
        { trade_date: "2026-08-30", valid: true, reason: null },
        {
          trade_date: "2026-08-31",
          valid: false,
          reason: "market_closed",
        },
      ],
    })
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage
          role="operator"
          search={deliveriesSearchSchema.parse({ tab: "backfills" })}
        />
      </QueryClientProvider>
    )
    await waitFor(() =>
      expect(screen.getByPlaceholderText("provider")).toBeInTheDocument()
    )
    fireEvent.change(screen.getByPlaceholderText("provider"), {
      target: { value: "taifex" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_futures_eod" },
    })
    fireEvent.change(screen.getByLabelText("回補起始日期"), {
      target: { value: "2026-08-30" },
    })
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-08-31" },
    })
    fireEvent.click(screen.getByRole("button", { name: "驗證交易日" }))
    await waitFor(() =>
      expect(
        screen.getByText("範圍有效；抓取與略過日期會在建立時凍結。")
      ).toBeInTheDocument()
    )
    expect(
      screen.getByText(/可建立 1 個抓取日期工作項目；略過 1 個依設定略過日期。/)
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("checkbox"))
    expect(screen.getByRole("button", { name: "建立回補" })).not.toBeDisabled()
  })

  it("allows an all-skipped request without pretending it acquired data", async () => {
    mocks.loadDashboard.mockResolvedValue(deliveriesResponse())
    mocks.previewHistoricalBackfill.mockResolvedValue({
      provider: "taifex",
      dataset_key: "tw_futures_eod",
      market: "TW",
      scope_valid: true,
      scope_reason: null,
      days: [
        { trade_date: "2026-08-31", valid: false, reason: "market_closed" },
      ],
    })
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage
          role="operator"
          search={deliveriesSearchSchema.parse({ tab: "backfills" })}
        />
      </QueryClientProvider>
    )
    await screen.findByPlaceholderText("provider")
    fireEvent.change(screen.getByPlaceholderText("provider"), {
      target: { value: "taifex" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_futures_eod" },
    })
    fireEvent.change(screen.getByLabelText("回補起始日期"), {
      target: { value: "2026-08-31" },
    })
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-08-31" },
    })
    fireEvent.click(screen.getByRole("button", { name: "驗證交易日" }))
    await screen.findByText("範圍有效；抓取與略過日期會在建立時凍結。")
    expect(screen.getByRole("checkbox")).not.toBeDisabled()
    expect(screen.getByRole("button", { name: "建立回補" })).toBeDisabled()
    expect(mocks.createHistoricalBackfill).not.toHaveBeenCalled()
  })

  it("allows default calendar dates in historical preview", async () => {
    mocks.loadDashboard.mockResolvedValue(deliveriesResponse())
    mocks.previewHistoricalBackfill.mockResolvedValue({
      provider: "taifex",
      dataset_key: "tw_futures_eod",
      market: "TW",
      scope_valid: true,
      scope_reason: null,
      days: [
        { trade_date: "2026-08-30", valid: true, reason: null },
        {
          trade_date: "2026-08-31",
          valid: true,
          reason: null,
        },
      ],
    })
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage
          role="operator"
          search={deliveriesSearchSchema.parse({ tab: "backfills" })}
        />
      </QueryClientProvider>
    )
    await screen.findByPlaceholderText("provider")
    fireEvent.change(screen.getByPlaceholderText("provider"), {
      target: { value: "taifex" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_futures_eod" },
    })
    fireEvent.change(screen.getByLabelText("回補起始日期"), {
      target: { value: "2026-08-30" },
    })
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-08-31" },
    })
    fireEvent.click(screen.getByRole("button", { name: "驗證交易日" }))
    await screen.findByText("範圍有效；抓取與略過日期會在建立時凍結。")
    expect(screen.getByRole("checkbox")).not.toBeDisabled()
    expect(mocks.createHistoricalBackfill).not.toHaveBeenCalled()
  })

  it("blocks confirmation and creation when the provider scope is invalid", async () => {
    mocks.loadDashboard.mockResolvedValue(deliveriesResponse())
    mocks.previewHistoricalBackfill.mockResolvedValue({
      provider: "taifex",
      dataset_key: "tw_futures_eod",
      market: "TW",
      scope_valid: false,
      scope_reason: "provider_scope_inactive",
      days: [
        {
          trade_date: "2026-08-31",
          valid: false,
          reason: "provider_scope_inactive",
        },
      ],
    })
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage
          role="operator"
          search={deliveriesSearchSchema.parse({ tab: "backfills" })}
        />
      </QueryClientProvider>
    )
    await screen.findByPlaceholderText("provider")
    fireEvent.change(screen.getByPlaceholderText("provider"), {
      target: { value: "taifex" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_futures_eod" },
    })
    fireEvent.change(screen.getByLabelText("回補起始日期"), {
      target: { value: "2026-08-31" },
    })
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-08-31" },
    })
    fireEvent.click(screen.getByRole("button", { name: "驗證交易日" }))
    await screen.findByText(
      "範圍無效：供應商與資料集的回補範圍尚未啟用（provider_scope_inactive）"
    )
    expect(screen.getByRole("checkbox")).toBeDisabled()
    expect(mocks.createHistoricalBackfill).not.toHaveBeenCalled()
  })

  it("shows localized guidance together with the original out-of-window error code", async () => {
    mocks.loadDashboard.mockResolvedValue(deliveriesResponse())
    mocks.previewHistoricalBackfill.mockResolvedValue({
      provider: "taifex",
      dataset_key: "tw_futures_eod",
      market: "TW",
      scope_valid: false,
      scope_reason: "outside_latest_31_days",
      days: [
        {
          trade_date: "2026-07-01",
          valid: false,
          reason: "outside_latest_31_days",
        },
      ],
    })
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage
          role="operator"
          search={deliveriesSearchSchema.parse({ tab: "backfills" })}
        />
      </QueryClientProvider>
    )
    await screen.findByPlaceholderText("provider")
    fireEvent.change(screen.getByPlaceholderText("provider"), {
      target: { value: "taifex" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_futures_eod" },
    })
    fireEvent.change(screen.getByLabelText("回補起始日期"), {
      target: { value: "2026-07-01" },
    })
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-07-01" },
    })
    fireEvent.click(screen.getByRole("button", { name: "驗證交易日" }))

    await screen.findByText(
      "範圍無效：日期範圍必須在最近 31 天內，且不可晚於今日（outside_latest_31_days）"
    )
    expect(screen.getByRole("checkbox")).toBeDisabled()
    expect(mocks.createHistoricalBackfill).not.toHaveBeenCalled()
  })

  it("keeps delivery and historical backfill pagination independent and only queries the visible tab", async () => {
    const response = deliveriesResponse()
    response.deliveries.data.pagination = {
      page: 2,
      page_size: 25,
      total_records: 75,
      total_pages: 3,
    }
    response.backfills.data.pagination = {
      page: 2,
      page_size: 25,
      total_records: 75,
      total_pages: 3,
    }
    mocks.loadDashboard.mockResolvedValue(response)
    const update = vi.fn()
    function Harness() {
      const [search, setSearch] = useState(() =>
        deliveriesSearchSchema.parse({
          tab: "alerts",
          p: 2,
          ps: 25,
          bp: 2,
          bps: 25,
        })
      )
      return (
        <DeliveriesPage
          role="operator"
          search={search}
          updateSearch={value => {
            setSearch(current => {
              const next = typeof value === "function" ? value(current) : value
              update(next)
              return next
            })
          }}
        />
      )
    }
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Harness />
      </QueryClientProvider>
    )
    await screen.findByRole("button", { name: "交付缺漏分頁下一頁" })
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "交付缺漏分頁下一頁" })
      ).not.toBeDisabled()
    )
    expect(
      mocks.loadDeliveryResource.mock.calls.every(([input]) =>
        ["alerts", "providerWork"].includes(input.data.resource)
      )
    ).toBe(true)
    fireEvent.click(screen.getByRole("button", { name: "交付缺漏分頁下一頁" }))
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({ p: 3, bp: 2 })
    )
    fireEvent.click(screen.getByRole("tab", { name: "歷史回補" }))
    await screen.findByRole("button", { name: "歷史回補請求分頁下一頁" })
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "歷史回補請求分頁下一頁" })
      ).not.toBeDisabled()
    )
    fireEvent.click(
      screen.getByRole("button", { name: "歷史回補請求分頁下一頁" })
    )
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({ p: 3, bp: 3 })
    )
  })

  it("clamps only the visible out-of-range paginator without changing hidden tab state", async () => {
    const response = deliveriesResponse()
    response.backfills.data.pagination = {
      page: 2,
      page_size: 25,
      total_records: 50,
      total_pages: 2,
    }
    mocks.loadDashboard.mockResolvedValue(response)
    const updateSearch = vi.fn()
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage
          role="operator"
          search={deliveriesSearchSchema.parse({
            tab: "backfills",
            p: 9,
            ps: 25,
            bp: 8,
            bps: 25,
          })}
          updateSearch={updateSearch}
        />
      </QueryClientProvider>
    )
    await waitFor(() =>
      expect(updateSearch).toHaveBeenCalledWith(
        expect.objectContaining({ p: 9, bp: 2 })
      )
    )
    expect(
      mocks.loadDeliveryResource.mock.calls.some(
        ([input]) => input.data.resource === "alerts"
      )
    ).toBe(false)
  })

  it("resets historical pagination against the latest search after a delayed successful create", async () => {
    const response = deliveriesResponse()
    response.deliveries.data.pagination = {
      page: 2,
      page_size: 50,
      total_records: 150,
      total_pages: 3,
    }
    response.backfills.data.pagination = {
      page: 2,
      page_size: 100,
      total_records: 300,
      total_pages: 3,
    }
    mocks.loadDashboard.mockResolvedValue(response)
    mocks.previewHistoricalBackfill.mockResolvedValue({
      provider: "taifex",
      dataset_key: "tw_futures_eod",
      market: "TW",
      scope_valid: true,
      scope_reason: null,
      days: [{ trade_date: "2026-08-30", valid: true, reason: null }],
    })
    let resolveCreate!: (value: object) => void
    mocks.createHistoricalBackfill.mockReturnValue(
      new Promise(resolve => {
        resolveCreate = resolve
      })
    )
    const updateSearch = vi.fn()
    const { rerender } = render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage
          role="operator"
          search={deliveriesSearchSchema.parse({
            p: 2,
            ps: 50,
            bp: 2,
            bps: 100,
            tab: "backfills",
          })}
          updateSearch={updateSearch}
        />
      </QueryClientProvider>
    )
    await screen.findByPlaceholderText("provider")
    fireEvent.change(screen.getByPlaceholderText("provider"), {
      target: { value: "taifex" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_futures_eod" },
    })
    fireEvent.change(screen.getByLabelText("回補起始日期"), {
      target: { value: "2026-08-30" },
    })
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-08-30" },
    })
    fireEvent.click(screen.getByRole("button", { name: "驗證交易日" }))
    await screen.findByText("範圍有效；抓取與略過日期會在建立時凍結。")
    fireEvent.click(screen.getByRole("checkbox"))
    fireEvent.click(screen.getByRole("button", { name: "建立回補" }))

    await waitFor(() =>
      expect(mocks.createHistoricalBackfill).toHaveBeenCalledWith({
        data: expect.objectContaining({
          provider: "taifex",
          datasetKey: "tw_futures_eod",
          startDate: "2026-08-30",
          endDate: "2026-08-30",
        }),
      })
    )
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage
          role="operator"
          search={deliveriesSearchSchema.parse({
            p: 3,
            ps: 100,
            bp: 2,
            bps: 50,
            tab: "backfills",
          })}
          updateSearch={updateSearch}
        />
      </QueryClientProvider>
    )
    resolveCreate({})
    await waitFor(() =>
      expect(updateSearch).toHaveBeenCalledWith(expect.any(Function))
    )
    const updater = updateSearch.mock.calls.at(-1)?.[0]
    expect(typeof updater).toBe("function")
    expect(
      (
        updater as (current: {
          p: number
          ps: number
          bp: number
          bps: number
        }) => { p: number; ps: number; bp: number; bps: number }
      )({
        p: 3,
        ps: 100,
        bp: 2,
        bps: 50,
      })
    ).toEqual({
      p: 3,
      ps: 100,
      bp: 1,
      bps: 50,
    })
  })

  it("switches from alerts to backfill without submitting, and keeps the draft across tabs", async () => {
    mocks.loadDashboard.mockResolvedValue(
      deliveriesResponse([makeMissingDelivery()])
    )
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage role="operator" />
      </QueryClientProvider>
    )
    expect(screen.getByRole("tab", { name: "交付計畫" })).toHaveAttribute(
      "aria-selected",
      "true"
    )
    fireEvent.click(screen.getByRole("tab", { name: "缺漏告警" }))
    fireEvent.click(
      await screen.findByRole("button", {
        name: "帶入 tw_futures_eod 2026-08-31 回補參數",
      })
    )
    expect(screen.getByRole("tab", { name: "歷史回補" })).toHaveAttribute(
      "aria-selected",
      "true"
    )
    expect(screen.getByPlaceholderText("provider")).toHaveValue("taifex")
    expect(screen.getByLabelText("回補起始日期")).toHaveValue("2026-08-31")
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-09-01" },
    })
    fireEvent.click(screen.getByRole("tab", { name: "交付計畫" }))
    fireEvent.click(screen.getByRole("tab", { name: "歷史回補" }))
    expect(screen.getByLabelText("回補結束日期")).toHaveValue("2026-09-01")
    expect(mocks.createHistoricalBackfill).not.toHaveBeenCalled()
    expect(screen.getByRole("button", { name: "建立回補" })).toBeDisabled()
  })

  it("ignores an old preview response after prefilling a different alert", async () => {
    let resolvePreview!: (value: {
      provider: string
      dataset_key: string
      market: string
      scope_valid: boolean
      scope_reason: null
      days: Array<{
        trade_date: string
        valid: boolean
        reason: null
      }>
    }) => void
    const pendingPreview = new Promise<Parameters<typeof resolvePreview>[0]>(
      resolve => {
        resolvePreview = resolve
      }
    )
    mocks.previewHistoricalBackfill.mockReturnValue(pendingPreview)
    mocks.loadDashboard.mockResolvedValue(
      deliveriesResponse([makeMissingDelivery()])
    )

    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage role="operator" />
      </QueryClientProvider>
    )

    fireEvent.click(screen.getByRole("tab", { name: "歷史回補" }))
    const provider = await screen.findByPlaceholderText("provider")
    fireEvent.change(provider, { target: { value: "finlab" } })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_equity_eod" },
    })
    fireEvent.change(screen.getByLabelText("回補起始日期"), {
      target: { value: "2026-08-29" },
    })
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-08-29" },
    })
    fireEvent.click(screen.getByRole("button", { name: "驗證交易日" }))
    await waitFor(() =>
      expect(mocks.previewHistoricalBackfill).toHaveBeenCalledTimes(1)
    )

    fireEvent.click(screen.getByRole("tab", { name: "缺漏告警" }))
    fireEvent.click(
      await screen.findByRole("button", {
        name: "帶入 tw_futures_eod 2026-08-31 回補參數",
      })
    )
    await act(async () => {
      resolvePreview({
        provider: "finlab",
        dataset_key: "tw_equity_eod",
        market: "TW",
        scope_valid: true,
        scope_reason: null,
        days: [{ trade_date: "2026-08-29", valid: true, reason: null }],
      })
      await pendingPreview
    })

    expect(
      screen.queryByText("範圍有效；抓取與略過日期會在建立時凍結。")
    ).not.toBeInTheDocument()
    expect(screen.getByRole("checkbox")).not.toBeChecked()
    expect(screen.getByRole("button", { name: "建立回補" })).toBeDisabled()
  })

  it("does not show a backfill action to viewers", async () => {
    mocks.loadDashboard.mockResolvedValue(
      deliveriesResponse([makeMissingDelivery()])
    )

    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage role="viewer" />
      </QueryClientProvider>
    )

    fireEvent.click(screen.getByRole("tab", { name: "缺漏告警" }))
    expect(await screen.findByText("tw_futures_eod")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", {
        name: "帶入 tw_futures_eod 2026-08-31 回補參數",
      })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("heading", { name: "供應商歷史回補" })
    ).not.toBeInTheDocument()
  })

  it("directs an overdue dated-universe alert to candidate review instead of backfill", async () => {
    mocks.loadDashboard.mockResolvedValue(
      deliveriesResponse([
        makeMissingDelivery({
          dataset_key: "tw_futures_eod",
          source: "taifex",
          schema_id: "futures_eod",
          expected_data_date: "2026-10-15",
          details: {
            code: "DATED_UNIVERSE_PENDING",
            severity: "error",
            candidate_count: 2,
            candidate_release_ids: [
              "019565d2-f838-7c91-85c1-72d4d7bbbe91",
              "019565d2-f838-7c91-85c1-72d4d7bbbe92",
            ],
            wait_age_seconds: 3600,
          },
        }),
      ])
    )

    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage
          role="operator"
          search={deliveriesSearchSchema.parse({ tab: "alerts" })}
        />
      </QueryClientProvider>
    )

    expect(
      await screen.findByText("Universe 等待發布（2）")
    ).toBeInTheDocument()
    expect(screen.getByText("請先審核候選")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", {
        name: "帶入 tw_futures_eod 2026-10-15 回補參數",
      })
    ).not.toBeInTheDocument()
  })
})
