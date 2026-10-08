import "@testing-library/jest-dom/vitest"

import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { useState, type ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type {
  DashboardRequest,
  DashboardResponse,
  DQIssue,
  MissingDelivery,
  RawPayload,
  Scheduler,
  SchedulerMutationResponse,
} from "../../lib/admin-api"
import { deliveriesSearchSchema } from "./operations.search"
import { marketFreshnessSchema } from "../../lib/admin-api"
import { OperationsRefreshProvider } from "../../components/OperationsRefresh"
import { ProtectedQueryScopeProvider } from "../../components/ProtectedQueryScope"

const mocks = vi.hoisted(() => ({
  loadDashboard: vi.fn(),
  loadRawPayloadDetail: vi.fn(),
  loadDeliveryPlans: vi.fn(),
  loadDeliveryPlanDatasets: vi.fn(),
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

import {
  DQPolicyDetails,
  DeliveriesPage,
  OperationsOverviewPage,
  IngestionOverviewPanel,
  SCHEDULER_RUNTIME_STATUS_META,
  SchedulerPanel,
  selectSchedulerRuntimeStatus,
} from "./OperationsConsole"
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
            provider: "shioaji",
            dataset_key: "tw_equity_minute",
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

function makeFullMarketFreshness(
  provider = "shioaji",
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
            blockers: [
              "activation_disabled",
              "calendar_does_not_cover_evaluation_date",
            ],
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
    dataset_key: "tw_equity_minute",
    source: "shioaji",
    schema_id: "market_minute",
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
    provider: "shioaji",
    dataset_key: "tw_equity_minute",
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

  it("selects scheduler runtime status independently from freshness", () => {
    const scheduler = makeScheduler({
      desired_state: "running",
      observed_state: "stopped",
    })
    expect(
      SCHEDULER_RUNTIME_STATUS_META[
        selectSchedulerRuntimeStatus({ control: scheduler, freshness: null })
      ].label
    ).toBe("執行中")
    expect(
      SCHEDULER_RUNTIME_STATUS_META[
        selectSchedulerRuntimeStatus({
          control: { ...scheduler, heartbeat_age_seconds: 91 },
          freshness: null,
        })
      ].label
    ).toBe("心跳過期")
  })

  it.each(["finlab", "shioaji", "taifex", "twelve_data"])(
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
    const freshness = makeFullMarketFreshness("shioaji", {
      activation_state: "activated",
      active_dataset_keys: ["tw_equity_minute"],
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

  it("retains calendar prerequisites and explains that fresh raw detail is not full-market proof", () => {
    const freshness = makeFullMarketFreshness("taifex")
    render(
      <QueryClientProvider client={new QueryClient()}>
        <IngestionOverviewPanel
          profile="full_market"
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
    expect(
      screen.getByText(
        /tw_futures_eod：activation_disabled；calendar_does_not_cover_evaluation_date/
      )
    ).toBeInTheDocument()
    expect(
      screen.getByText(/資料更新不代表全市場已啟用或此 Scheduler 已回報心跳/)
    ).toBeInTheDocument()
    expect(screen.getByText(/此全市場 runtime 尚未回報/)).toBeInTheDocument()
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
            profile="full_market"
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
        scheduler_key: "scheduler-shioaji",
        provider: "shioaji",
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
      name: "Pilot 全部停止",
    })
    expect(
      screen.getByRole("button", { name: "Pilot 全部啟動" })
    ).toBeDisabled()
    fireEvent.click(stopAllButton)
    expect(
      screen.getByRole("heading", { name: "確認 Pilot 全部停止？" })
    ).toBeInTheDocument()
    expect(
      screen.getByText("finlab / scheduler-finlab / r7")
    ).toBeInTheDocument()
    expect(
      screen.getByText("shioaji / scheduler-shioaji / r11")
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
      screen.queryByRole("button", { name: "Pilot 全部停止" })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Pilot 全部啟動" })
    ).not.toBeInTheDocument()
  })

  it("keeps scheduler controls owner-only", () => {
    const scheduler = makeScheduler()
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerPanel
          result={{ ok: true, data: { success: true, data: [scheduler] } }}
          loading={false}
          pending={false}
          refreshError=""
          role="viewer"
        />
      </QueryClientProvider>
    )
    expect(
      screen.getByText("唯讀：只有 owner 可以變更排程狀態。")
    ).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Pilot 全部停止" })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Pilot 全部啟動" })
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
        scheduler_key: "scheduler-shioaji",
        provider: "shioaji",
        desired_state: "running",
        observed_state: "running",
        revision: 11,
      }),
    ]
    schedulers.push(
      makeScheduler({
        scheduler_key: "full_market_finlab_v1",
        desired_state: "running",
        start_allowed: true,
      })
    )
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
        <SchedulerPanel
          result={{ ok: true, data: { success: true, data: schedulers } }}
          loading={false}
          pending={false}
          refreshError=""
          role="owner"
        />
      </QueryClientProvider>
    )

    expect(screen.getByText(/部署前人工確認：/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Pilot 全部停止" }))
    expect(
      screen.getByRole("heading", { name: "確認 Pilot 全部停止？" })
    ).toBeInTheDocument()
    expect(
      screen.getByText("finlab / scheduler-finlab / r7")
    ).toBeInTheDocument()
    expect(
      screen.getByText("shioaji / scheduler-shioaji / r11")
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "確認 Pilot 全部停止" }))

    await waitFor(() => expect(mocks.updateScheduler).toHaveBeenCalledTimes(1))
    expect(
      screen.getByRole("button", { name: "Full market 全部停止" })
    ).toBeDisabled()
    expect(
      screen.getByRole("button", {
        name: "Full market finlab full_market_finlab_v1 設為已停止",
      })
    ).toBeDisabled()
    expect(
      screen.getByRole("button", { name: "Pilot 全部停止" })
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
        schedulerKey: "scheduler-shioaji",
        desiredState: "stopped",
        expectedRevision: 11,
      },
    })
  })

  it("starts only eligible schedulers and explains skipped full-market controls", async () => {
    const schedulers = [
      makeScheduler({ scheduler_key: "bounded", desired_state: "stopped" }),
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
        scheduler_key: "full_market_shioaji_v1",
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
        <SchedulerPanel
          result={{ ok: true, data: { success: true, data: schedulers } }}
          loading={false}
          pending={false}
          refreshError=""
          role="owner"
        />
      </QueryClientProvider>
    )
    expect(
      screen.getByText(/全部啟動將跳過 2 個 Scheduler/)
    ).toBeInTheDocument()
    expect(
      screen.getByRole("button", {
        name: "Full market twelve_data full_market_twelve_data_v1 設為執行中",
      })
    ).toBeDisabled()
    fireEvent.click(
      screen.getByRole("button", { name: "Full market 全部啟動" })
    )
    expect(screen.getByText(/將依序更新 1 個/)).toBeInTheDocument()
    expect(screen.getByText(/跳過 2 個無法啟動/)).toBeInTheDocument()
    fireEvent.click(
      screen.getByRole("button", { name: "確認 Full market 全部啟動" })
    )
    await waitFor(() => expect(mocks.updateScheduler).toHaveBeenCalledTimes(1))
    expect(
      mocks.updateScheduler.mock.calls.map(call => call[0].data.schedulerKey)
    ).toEqual(["full_market_finlab_v1"])
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
        scheduler_key: "full_market_shioaji_v1",
        provider: "shioaji",
        desired_state: "running",
        start_allowed: false,
        start_blockers: ["full_market_no_enabled_datasets"],
      }),
    ]
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerPanel
          result={{ ok: true, data: { success: true, data: schedulers } }}
          loading={false}
          pending={false}
          refreshError=""
          role="owner"
        />
      </QueryClientProvider>
    )
    expect(
      screen.getByRole("button", { name: "Full market 全部啟動" })
    ).toBeDisabled()
    expect(
      screen.getByRole("button", {
        name: "Full market finlab full_market_finlab_v1 設為執行中",
      })
    ).toBeDisabled()
    expect(
      screen.getByRole("button", {
        name: "Full market shioaji full_market_shioaji_v1 設為已停止",
      })
    ).toBeEnabled()
    fireEvent.click(
      screen.getByRole("button", { name: "Full market 全部停止" })
    )
    expect(screen.getByText(/將依序更新 1 個/)).toBeInTheDocument()
    expect(
      screen.getByText(/shioaji \/ full_market_shioaji_v1/)
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
        "全市場啟動條件已變更或尚未完成，請重新整理並確認 feed 啟用、readiness、baseline 與日曆。"
      )
    )
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerPanel
          result={{ ok: true, data: { success: true, data: [scheduler] } }}
          loading={false}
          pending={false}
          refreshError=""
          role="owner"
        />
      </QueryClientProvider>
    )
    fireEvent.click(
      screen.getByRole("button", {
        name: "Full market finlab full_market_finlab_v1 設為執行中",
      })
    )
    fireEvent.click(screen.getByRole("button", { name: "確認啟用" }))
    expect(await screen.findByText(/全市場啟動條件已變更/)).toBeInTheDocument()
    expect(
      screen.queryByText(/排程版本已被其他使用者更新/)
    ).not.toBeInTheDocument()
  })

  it.each([
    [
      "Pilot",
      "pilot_finlab",
      "Full market",
      "full_market_finlab_v1",
      "stopped",
      "running",
    ],
    [
      "Full market",
      "full_market_finlab_v1",
      "Pilot",
      "pilot_finlab",
      "stopped",
      "running",
    ],
    [
      "Pilot",
      "pilot_finlab",
      "Full market",
      "full_market_finlab_v1",
      "running",
      "stopped",
    ],
    [
      "Full market",
      "full_market_finlab_v1",
      "Pilot",
      "pilot_finlab",
      "running",
      "stopped",
    ],
  ] as const)(
    "warns before %s %s single and bulk starts with %s %s desired=%s observed=%s",
    async (
      profile,
      key,
      otherProfile,
      otherKey,
      desiredState,
      observedState
    ) => {
      const rows = [
        makeScheduler({ scheduler_key: key, start_allowed: true }),
        makeScheduler({
          scheduler_key: otherKey,
          desired_state: desiredState,
          observed_state: observedState,
          start_allowed: true,
        }),
      ]
      mocks.updateScheduler.mockResolvedValue({
        success: true,
        data: { ...rows[0]!, desired_state: "running", revision: 2 },
      })
      render(
        <QueryClientProvider client={new QueryClient()}>
          <IngestionOverviewPanel
            profile={profile === "Full market" ? "full_market" : "pilot"}
            freshnessResult={null}
            schedulersResult={{ ok: true, data: { success: true, data: rows } }}
            loading={false}
            pending={false}
            freshnessError=""
            schedulersError=""
            role="owner"
          />
        </QueryClientProvider>
      )
      fireEvent.click(
        screen.getByRole("button", {
          name: `${profile} finlab ${key} 設為執行中`,
        })
      )
      expect(
        screen.getByText("Full market 與 Pilot 將同時執行")
      ).toBeInTheDocument()
      expect(mocks.updateScheduler).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole("button", { name: "取消" }))
      fireEvent.click(
        screen.getByRole("button", { name: `${profile} 全部啟動` })
      )
      expect(
        screen.getByText("Full market 與 Pilot 將同時執行")
      ).toBeInTheDocument()
      expect(mocks.updateScheduler).not.toHaveBeenCalled()
      fireEvent.click(
        screen.getByRole("button", { name: `確認 ${profile} 全部啟動` })
      )
      await waitFor(() =>
        expect(mocks.updateScheduler).toHaveBeenCalledTimes(1)
      )
      expect(mocks.updateScheduler).toHaveBeenCalledWith({
        data: {
          schedulerKey: key,
          desiredState: "running",
          expectedRevision: 1,
        },
      })
      expect(
        screen.queryByRole("region", { name: `${otherProfile} 排程` })
      ).not.toBeInTheDocument()
    }
  )

  it.each(["Pilot", "Full market"])(
    "scopes %s bulk stops to its own overview section",
    async profile => {
      const rows = [
        makeScheduler({
          scheduler_key: "pilot_finlab",
          desired_state: "running",
          observed_state: "running",
        }),
        makeScheduler({
          scheduler_key: "full_market_finlab_v1",
          desired_state: "running",
          observed_state: "running",
          start_allowed: true,
        }),
      ]
      const key = profile === "Pilot" ? "pilot_finlab" : "full_market_finlab_v1"
      mocks.updateScheduler.mockResolvedValue({
        success: true,
        data: {
          ...rows.find(row => row.scheduler_key === key)!,
          desired_state: "stopped",
          revision: 2,
        },
      })
      render(
        <QueryClientProvider client={new QueryClient()}>
          <IngestionOverviewPanel
            profile={profile === "Full market" ? "full_market" : "pilot"}
            freshnessResult={null}
            schedulersResult={{ ok: true, data: { success: true, data: rows } }}
            loading={false}
            pending={false}
            freshnessError=""
            schedulersError=""
            role="owner"
          />
        </QueryClientProvider>
      )
      const section = screen.getByRole("region", { name: `${profile} 排程` })
      expect(within(section).getByText(key)).toBeInTheDocument()
      expect(
        within(section).queryByText(
          profile === "Pilot" ? "full_market_finlab_v1" : "pilot_finlab"
        )
      ).not.toBeInTheDocument()
      fireEvent.click(
        within(section).getByRole("button", { name: `${profile} 全部停止` })
      )
      expect(
        screen.queryByText("Full market 與 Pilot 將同時執行")
      ).not.toBeInTheDocument()
      expect(mocks.updateScheduler).not.toHaveBeenCalled()
      fireEvent.click(
        screen.getByRole("button", { name: `確認 ${profile} 全部停止` })
      )
      await waitFor(() =>
        expect(mocks.updateScheduler).toHaveBeenCalledTimes(1)
      )
      expect(mocks.updateScheduler).toHaveBeenCalledWith({
        data: {
          schedulerKey: key,
          desiredState: "stopped",
          expectedRevision: 1,
        },
      })
    }
  )

  it("keeps freshness-only cards out of bulk writes and warns using live fallback status", () => {
    const rows = [makeScheduler({ scheduler_key: "pilot_finlab" })]
    const props = {
      freshnessResult: {
        ok: true as const,
        data: {
          success: true as const,
          data: [makeFullMarketFreshness("finlab")],
        },
      },
      schedulersResult: {
        ok: true as const,
        data: { success: true as const, data: rows },
      },
      loading: false,
      pending: false,
      freshnessError: "",
      schedulersError: "",
      role: "owner" as const,
    }
    const client = new QueryClient()
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <IngestionOverviewPanel {...props} />
      </QueryClientProvider>
    )
    expect(
      screen.queryByRole("region", { name: "Full market 排程" })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Full market 全部啟動" })
    ).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Pilot 全部啟動" }))
    expect(
      screen.queryByText("Full market 與 Pilot 將同時執行")
    ).not.toBeInTheDocument()
    rerender(
      <QueryClientProvider client={client}>
        <IngestionOverviewPanel
          {...props}
          freshnessResult={{
            ok: true,
            data: {
              success: true,
              data: [
                makeFullMarketFreshness("finlab", {
                  observed_state: "running",
                }),
              ],
            },
          }}
        />
      </QueryClientProvider>
    )
    expect(
      screen.getByText("Full market 與 Pilot 將同時執行")
    ).toBeInTheDocument()
    expect(screen.getByText(/將依序更新 1 個/)).toBeInTheDocument()
    expect(mocks.updateScheduler).not.toHaveBeenCalled()
  })

  it("does not warn for unrelated providers or blocked bulk targets", () => {
    const rows = [
      makeScheduler({
        scheduler_key: "pilot_finlab",
        desired_state: "running",
        observed_state: "running",
      }),
      makeScheduler({
        scheduler_key: "full_market_finlab_v1",
        start_allowed: false,
      }),
      makeScheduler({
        scheduler_key: "full_market_shioaji_v1",
        provider: "shioaji",
        start_allowed: true,
      }),
    ]
    render(
      <QueryClientProvider client={new QueryClient()}>
        <SchedulerPanel
          result={{ ok: true, data: { success: true, data: rows } }}
          loading={false}
          pending={false}
          refreshError=""
          role="owner"
        />
      </QueryClientProvider>
    )
    fireEvent.click(
      screen.getByRole("button", {
        name: "Full market shioaji full_market_shioaji_v1 設為執行中",
      })
    )
    expect(
      screen.queryByText("Full market 與 Pilot 將同時執行")
    ).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "取消" }))
    fireEvent.click(
      screen.getByRole("button", { name: "Full market 全部啟動" })
    )
    expect(
      screen.queryByText("Full market 與 Pilot 將同時執行")
    ).not.toBeInTheDocument()
    expect(screen.getByText(/跳過 1 個無法啟動/)).toBeInTheDocument()
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
        provider: "shioaji",
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
        <SchedulerPanel
          result={{ ok: true, data: { success: true, data: schedulers } }}
          loading={false}
          pending={false}
          refreshError=""
          role="owner"
        />
      </QueryClientProvider>
    )

    fireEvent.click(screen.getByRole("button", { name: "Pilot 全部停止" }))
    fireEvent.click(screen.getByRole("button", { name: "確認 Pilot 全部停止" }))

    await waitFor(() => expect(mocks.updateScheduler).toHaveBeenCalledTimes(2))
    expect(
      screen.getByText("排程版本已被其他使用者更新，請先重新整理後再試。")
    ).toBeInTheDocument()
  })

  it("shows closed dates as skipped and allows confirmation-based backfill creation", async () => {
    mocks.loadDashboard.mockResolvedValue(deliveriesResponse())
    mocks.previewHistoricalBackfill.mockResolvedValue({
      provider: "shioaji",
      dataset_key: "tw_equity_minute",
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
      target: { value: "shioaji" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_equity_minute" },
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
        screen.getByText("範圍有效；休市日期會略過，只為開市日期建立工作項目。")
      ).toBeInTheDocument()
    )
    expect(
      screen.getByText(/可建立 1 個開市日期工作項目；略過 1 個休市日期。/)
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("checkbox"))
    expect(screen.getByRole("button", { name: "建立回補" })).not.toBeDisabled()
  })

  it("blocks an all-closed preview from confirmation and creation", async () => {
    mocks.loadDashboard.mockResolvedValue(deliveriesResponse())
    mocks.previewHistoricalBackfill.mockResolvedValue({
      provider: "shioaji",
      dataset_key: "tw_equity_minute",
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
      target: { value: "shioaji" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_equity_minute" },
    })
    fireEvent.change(screen.getByLabelText("回補起始日期"), {
      target: { value: "2026-08-31" },
    })
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-08-31" },
    })
    fireEvent.click(screen.getByRole("button", { name: "驗證交易日" }))
    await screen.findByText("範圍無可執行交易日；整個回補請求將被拒絕。")
    expect(screen.getByRole("checkbox")).toBeDisabled()
    expect(screen.getByRole("button", { name: "建立回補" })).toBeDisabled()
    expect(mocks.createHistoricalBackfill).not.toHaveBeenCalled()
  })

  it("blocks previews containing an unpublished date even when another date is open", async () => {
    mocks.loadDashboard.mockResolvedValue(deliveriesResponse())
    mocks.previewHistoricalBackfill.mockResolvedValue({
      provider: "shioaji",
      dataset_key: "tw_equity_minute",
      market: "TW",
      scope_valid: true,
      scope_reason: null,
      days: [
        { trade_date: "2026-08-30", valid: true, reason: null },
        {
          trade_date: "2026-08-31",
          valid: false,
          reason: "calendar_unpublished",
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
      target: { value: "shioaji" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_equity_minute" },
    })
    fireEvent.change(screen.getByLabelText("回補起始日期"), {
      target: { value: "2026-08-30" },
    })
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-08-31" },
    })
    fireEvent.click(screen.getByRole("button", { name: "驗證交易日" }))
    await screen.findByText("範圍含未發布交易日；整個回補請求將被拒絕。")
    expect(screen.getByRole("checkbox")).toBeDisabled()
    expect(mocks.createHistoricalBackfill).not.toHaveBeenCalled()
  })

  it("blocks confirmation and creation when the provider scope is invalid", async () => {
    mocks.loadDashboard.mockResolvedValue(deliveriesResponse())
    mocks.previewHistoricalBackfill.mockResolvedValue({
      provider: "shioaji",
      dataset_key: "tw_equity_minute",
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
      target: { value: "shioaji" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_equity_minute" },
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
      provider: "shioaji",
      dataset_key: "tw_equity_minute",
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
      target: { value: "shioaji" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_equity_minute" },
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
      mocks.loadDeliveryResource.mock.calls.every(
        ([input]) => input.data.resource === "alerts"
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
      provider: "shioaji",
      dataset_key: "tw_equity_minute",
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
      target: { value: "shioaji" },
    })
    fireEvent.change(screen.getByPlaceholderText("dataset_key"), {
      target: { value: "tw_equity_minute" },
    })
    fireEvent.change(screen.getByLabelText("回補起始日期"), {
      target: { value: "2026-08-30" },
    })
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-08-30" },
    })
    fireEvent.click(screen.getByRole("button", { name: "驗證交易日" }))
    await screen.findByText("範圍有效；所有日期都會建立工作項目。")
    fireEvent.click(screen.getByRole("checkbox"))
    fireEvent.click(screen.getByRole("button", { name: "建立回補" }))

    await waitFor(() =>
      expect(mocks.createHistoricalBackfill).toHaveBeenCalledWith({
        data: expect.objectContaining({
          provider: "shioaji",
          datasetKey: "tw_equity_minute",
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
    expect(screen.getByRole("tab", { name: "全市場計畫" })).toHaveAttribute(
      "aria-selected",
      "true"
    )
    fireEvent.click(screen.getByRole("tab", { name: "缺漏告警" }))
    fireEvent.click(
      await screen.findByRole("button", {
        name: "帶入 tw_equity_minute 2026-08-31 回補參數",
      })
    )
    expect(screen.getByRole("tab", { name: "歷史回補" })).toHaveAttribute(
      "aria-selected",
      "true"
    )
    expect(screen.getByPlaceholderText("provider")).toHaveValue("shioaji")
    expect(screen.getByLabelText("回補起始日期")).toHaveValue("2026-08-31")
    fireEvent.change(screen.getByLabelText("回補結束日期"), {
      target: { value: "2026-09-01" },
    })
    fireEvent.click(screen.getByRole("tab", { name: "全市場計畫" }))
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
        name: "帶入 tw_equity_minute 2026-08-31 回補參數",
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
      screen.queryByText("範圍有效；所有日期都會建立工作項目。")
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
    expect(await screen.findByText("tw_equity_minute")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", {
        name: "帶入 tw_equity_minute 2026-08-31 回補參數",
      })
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("heading", { name: "供應商歷史回補" })
    ).not.toBeInTheDocument()
  })
})
