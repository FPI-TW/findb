import "@testing-library/jest-dom/vitest"

import {
  act,
  cleanup,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import type {
  DashboardRequest,
  DashboardResponse,
  DQIssue,
  RawPayload,
  Scheduler,
  SchedulerMutationResponse,
} from "../../lib/admin-api"
import { ProtectedQueryScopeProvider } from "../../components/ProtectedQueryScope"

const mocks = vi.hoisted(() => ({
  loadDashboard: vi.fn(),
  loadRawPayloadDetail: vi.fn(),
  updateScheduler: vi.fn(),
  navigate: vi.fn(),
}))

vi.mock("@tanstack/react-router", () => ({
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
  OperationsOverviewPage,
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
        {children}
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
})

afterEach(() => {
  vi.useRealTimers()
  cleanup()
})

describe("Operations query hooks", () => {
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

  it("updates the overview cache immediately and invalidates the same key after mutation", async () => {
    const queryClient = new QueryClient()
    const current = {
      ...overviewResponse([makeScheduler()]),
      refreshErrors: ["", "", ""],
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
  })
})
