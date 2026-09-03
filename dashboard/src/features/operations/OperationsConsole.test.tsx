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
import type { ReactNode } from "react"
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
import { ProtectedQueryScopeProvider } from "../../components/ProtectedQueryScope"

const mocks = vi.hoisted(() => ({
  loadDashboard: vi.fn(),
  loadRawPayloadDetail: vi.fn(),
  updateScheduler: vi.fn(),
  createHistoricalBackfill: vi.fn(),
  cancelHistoricalBackfill: vi.fn(),
  previewHistoricalBackfill: vi.fn(),
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
  DeliveriesPage,
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
      name: "全部停止",
    })
    expect(screen.getByRole("button", { name: "全部啟動" })).toBeDisabled()
    fireEvent.click(stopAllButton)
    expect(
      screen.getByRole("heading", { name: "確認全部停止 Scheduler？" })
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
        scheduler_key: "scheduler-shioaji",
        provider: "shioaji",
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
        <SchedulerPanel
          result={{ ok: true, data: { success: true, data: schedulers } }}
          loading={false}
          pending={false}
          refreshError=""
          role="owner"
        />
      </QueryClientProvider>
    )

    expect(screen.getByText("部署前人工確認")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "全部停止" }))
    expect(
      screen.getByRole("heading", { name: "確認全部停止 Scheduler？" })
    ).toBeInTheDocument()
    expect(
      screen.getByText("finlab / scheduler-finlab / r7")
    ).toBeInTheDocument()
    expect(
      screen.getByText("shioaji / scheduler-shioaji / r11")
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "確認全部停止" }))

    await waitFor(() => expect(mocks.updateScheduler).toHaveBeenCalledTimes(1))
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
        <DeliveriesPage role="operator" />
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
        <DeliveriesPage role="operator" />
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
        <DeliveriesPage role="operator" />
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
        <DeliveriesPage role="operator" />
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
        <DeliveriesPage role="operator" />
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

  it("keeps delivery and historical backfill pagination independent", async () => {
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
    const updateSearch = vi.fn()
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage
          role="operator"
          search={{ p: 2, ps: 25, bp: 2, bps: 25 }}
          updateSearch={updateSearch}
        />
      </QueryClientProvider>
    )

    await screen.findByRole("navigation", { name: "交付缺漏分頁" })
    fireEvent.click(screen.getByRole("button", { name: "交付缺漏分頁下一頁" }))
    fireEvent.click(
      screen.getByRole("button", { name: "歷史回補請求分頁下一頁" })
    )

    expect(updateSearch).toHaveBeenCalledWith({
      p: 3,
      ps: 25,
      bp: 2,
      bps: 25,
    })
    expect(updateSearch).toHaveBeenCalledWith({
      p: 2,
      ps: 25,
      bp: 3,
      bps: 25,
    })
  })

  it("clamps both out-of-range paginators together in one search update", async () => {
    const response = deliveriesResponse()
    response.deliveries.data.pagination = {
      page: 3,
      page_size: 25,
      total_records: 75,
      total_pages: 3,
    }
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
          search={{ p: 9, ps: 25, bp: 8, bps: 25 }}
          updateSearch={updateSearch}
        />
      </QueryClientProvider>
    )
    await waitFor(() =>
      expect(updateSearch).toHaveBeenCalledWith({
        p: 3,
        ps: 25,
        bp: 2,
        bps: 25,
      })
    )
    expect(updateSearch).toHaveBeenCalledTimes(1)
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
          search={{ p: 2, ps: 50, bp: 2, bps: 100 }}
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
          search={{ p: 3, ps: 100, bp: 2, bps: 50 }}
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

  it("spaces delivery sections and prefills backfill inputs from an alert", async () => {
    mocks.loadDashboard.mockResolvedValue(
      deliveriesResponse([makeMissingDelivery()])
    )

    render(
      <QueryClientProvider client={new QueryClient()}>
        <DeliveriesPage role="operator" />
      </QueryClientProvider>
    )

    const openAlertsPanel = (
      await screen.findByRole("heading", {
        name: "未解決的交付缺漏",
      })
    ).closest('[data-slot="card"]')
    const backfillPanel = screen
      .getByRole("heading", { name: "供應商歷史回補" })
      .closest('[data-slot="card"]')
    expect(openAlertsPanel).not.toBeNull()
    expect(backfillPanel).toHaveClass("mt-5")

    const prefillButton = await screen.findByRole("button", {
      name: "帶入 tw_equity_minute 2026-08-31 回補參數",
    })

    expect(
      screen.getByRole("heading", { name: "選擇回補範圍" })
    ).toBeInTheDocument()
    expect(
      screen.getByRole("heading", { name: "驗證交易日" })
    ).toBeInTheDocument()
    expect(
      screen.getByRole("heading", { name: "確認並建立" })
    ).toBeInTheDocument()
    const createButton = screen.getByRole("button", { name: "建立回補" })
    expect(createButton).toHaveAttribute("data-variant", "default")
    expect(createButton).toHaveAttribute("data-size", "lg")

    expect(prefillButton).toHaveAttribute("data-variant", "outline")
    fireEvent.click(prefillButton)

    expect(screen.getByPlaceholderText("provider")).toHaveValue("shioaji")
    expect(screen.getByPlaceholderText("dataset_key")).toHaveValue(
      "tw_equity_minute"
    )
    expect(screen.getByLabelText("回補起始日期")).toHaveValue("2026-08-31")
    expect(screen.getByLabelText("回補結束日期")).toHaveValue("2026-08-31")
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

    fireEvent.click(
      screen.getByRole("button", {
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
