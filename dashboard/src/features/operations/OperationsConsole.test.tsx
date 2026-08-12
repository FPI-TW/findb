import "@testing-library/jest-dom/vitest"

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { toast, Toaster } from "../../components/ui/toast"
import type {
  DashboardResponse,
  DQIssue,
  MarketFreshness,
  RawPayload,
  Scheduler,
  SchedulerMutationResponse,
  SchedulersResponse,
  OperationsView,
} from "../../lib/admin-api"

const mocks = vi.hoisted(() => ({
  loadDashboard: vi.fn(),
  loadRawPayloadDetail: vi.fn(),
  navigate: vi.fn(),
  pathname: "/operations",
  updateScheduler: vi.fn(),
}))

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
  Outlet: () => null,
  useNavigate: () => mocks.navigate,
  useRouterState: ({
    select,
  }: {
    select: (state: { location: { pathname: string } }) => string
  }) => select({ location: { pathname: mocks.pathname } }),
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
  default as OperationsLayout,
  IngestionOverviewPanel,
  OperationsContext,
  QualityPage,
  RawPayloadsPage,
  SchedulerPanel,
  SCHEDULER_RUNTIME_STATUS_META,
  selectSchedulerRuntimeStatus,
  operationsViewForPathname,
  type OperationsContextValue,
} from "./OperationsConsole"

const timestamp = "2026-08-04T02:00:00Z"

function makeScheduler(overrides: Partial<Scheduler> = {}): Scheduler {
  return {
    scheduler_key: "twelve_data_us_common_stocks_daily_v1",
    provider: "twelve_data",
    dataset_keys: ["us_equity_eod"],
    slot_id: "western_markets_window",
    scheduled_local_time: "06:30:00",
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

function panelResult(rows: Scheduler[]): {
  ok: true
  data: SchedulersResponse
} {
  return { ok: true, data: { success: true, data: rows } }
}

function makeFreshness(
  overrides: Partial<MarketFreshness> = {}
): MarketFreshness {
  return {
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
    heartbeat_age_seconds: 5,
    configuration_status: "ready",
    configuration_errors: [],
    status: "fresh",
    expected_data_date: "2026-08-04",
    coverage_data_date: "2026-08-04",
    last_fetched_at: timestamp,
    last_successful_update_at: timestamp,
    last_complete_at: timestamp,
    next_scheduled_at: timestamp,
    feed_count: 1,
    fresh_feed_count: 1,
    late_feed_count: 0,
    feeds: [
      {
        dataset_key: "tw_equity_eod",
        source: "finlab",
        schema_id: "market_eod",
        schema_version: 1,
        expected_data_date: "2026-08-04",
        latest_successful_data_date: "2026-08-04",
        last_fetched_at: timestamp,
        last_completed_at: timestamp,
        last_run_id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
        total_records: 100,
        success_records: 100,
        failed_records: 0,
        policy_outcome: "pass",
        open_missing_delivery_alert: false,
        last_failure_code: null,
        configuration_error: null,
        status: "fresh",
      },
    ],
    ...overrides,
  }
}

function freshnessResult(rows: MarketFreshness[]) {
  return {
    ok: true as const,
    data: { success: true as const, data: rows },
  }
}

function qualityIssue(): DQIssue {
  return {
    id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
    run_id: "019565d2-f838-7c91-85c1-72d4d7bbbe98",
    instrument_id: null,
    trade_date: "2026-08-04",
    issue_type: "price_gap",
    severity: "error",
    description: "close differs from policy",
    source: "source-api",
    provider: "finlab",
    dataset_key: "tw_equity_eod",
    schema_id: "market_eod",
    schema_version: 1,
    raw_payload_id: "019565d2-f838-7c91-85c1-72d4d7bbbe99",
    raw_available: true,
    fetched_at: "2026-08-04T02:00:00Z",
    request_key: "req-bounded-1",
    batch_data_date: "2026-08-04",
    policy_detail: {
      code: "close_gap",
      action: "quarantine",
      reason: "provider close differs from expected close",
      observed: 101,
      expected: 100,
      violations: [
        {
          code: "close_gap",
          action: "quarantine",
          reason: "close differs",
          observed: 101,
          expected: 100,
          secret: "raw-payload-secret-must-not-render",
        },
        {
          code: "volume_gap",
          action: "review",
          reason: "volume differs",
          observed: 8,
          expected: 10,
        },
      ],
      violation_count: 12,
      truncated: true,
      raw_payload: { secret: "raw-payload-secret-2" },
    },
    resolved: false,
    created_at: "2026-08-04T02:01:00Z",
  }
}

function qualityDashboardData(issue: DQIssue): DashboardResponse {
  return {
    view: "quality",
    fetchedAt: timestamp,
    issues: {
      ok: true,
      data: {
        data: [issue],
        pagination: {
          page: 1,
          page_size: 25,
          total_records: 1,
          total_pages: 1,
        },
      },
    },
  }
}

function makeRawPayload(overrides: Partial<RawPayload> = {}): RawPayload {
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
    ...overrides,
  }
}

function rawDashboardData(
  payloads: RawPayload[] = [makeRawPayload()],
  totalPages = 1
): DashboardResponse {
  return {
    view: "rawPayloads",
    fetchedAt: timestamp,
    rawPayloads: {
      ok: true,
      data: {
        data: payloads.map(payload => ({ ...payload, payload: null })),
        pagination: {
          page: 1,
          page_size: 25,
          total_records: payloads.length,
          total_pages: totalPages,
        },
      },
    },
  }
}

function dashboardForView(view: OperationsView): DashboardResponse {
  if (view === "overview") {
    return {
      view,
      fetchedAt: timestamp,
      freshness: { ok: false, error: "fixture" },
      queue: { ok: false, error: "fixture" },
      schedulers: { ok: false, error: "fixture" },
    }
  }
  if (view === "deliveries") {
    return {
      view,
      fetchedAt: timestamp,
      deliveries: {
        ok: true,
        data: {
          data: [],
          pagination: {
            page: 1,
            page_size: 100,
            total_records: 0,
            total_pages: 0,
          },
        },
      },
    }
  }
  if (view === "quality") return qualityDashboardData(qualityIssue())
  if (view === "corrections") {
    return {
      view,
      fetchedAt: timestamp,
      corrections: {
        ok: true,
        data: {
          data: [],
          pagination: {
            page: 1,
            page_size: 50,
            total_records: 0,
            total_pages: 0,
          },
        },
      },
    }
  }
  return rawDashboardData()
}

function qualityContextValue(issue: DQIssue): OperationsContextValue {
  return {
    data: qualityDashboardData(issue),
    error: "",
    freshnessError: "",
    schedulersError: "",
    pending: false,
    initialLoading: false,
    role: "viewer",
    applyScheduler: vi.fn(),
    filters: {
      datasetKey: "",
      runId: "",
      dateFrom: "",
      dateTo: "",
      page: 1,
      pageSize: 25,
    },
    setFilters: vi.fn(),
    refresh: vi.fn().mockResolvedValue(undefined),
  }
}

function renderPanel(
  rows: Scheduler[],
  options: {
    role?: "owner" | "operator" | "viewer"
    loading?: boolean
    pending?: boolean
    refreshError?: string
    applyScheduler?: (response: SchedulerMutationResponse) => void
  } = {}
) {
  return render(
    <>
      <SchedulerPanel
        result={panelResult(rows)}
        loading={options.loading ?? false}
        pending={options.pending ?? false}
        refreshError={options.refreshError ?? ""}
        role={options.role ?? "owner"}
        {...(options.applyScheduler
          ? { applyScheduler: options.applyScheduler }
          : {})}
      />
      <Toaster />
    </>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.pathname = "/operations"
  mocks.loadDashboard.mockImplementation(
    async ({ data }: { data: { view: OperationsView } }) =>
      dashboardForView(data.view)
  )
  mocks.loadRawPayloadDetail.mockResolvedValue(makeRawPayload())
})

afterEach(() => {
  vi.useRealTimers()
  toast.dismiss()
  cleanup()
})

describe("operations authentication", () => {
  it("returns to login when a dashboard refresh reports an expired session", async () => {
    mocks.loadDashboard.mockRejectedValue(
      new Error("Dashboard authentication required")
    )

    render(<OperationsLayout username="operator" role="operator" />)

    await waitFor(() =>
      expect(mocks.navigate).toHaveBeenCalledWith({
        to: "/login",
        replace: true,
      })
    )
    expect(screen.queryByText("無法更新營運資料")).not.toBeInTheDocument()
  })

  it("returns to login when a scheduler mutation reports an expired session", async () => {
    mocks.updateScheduler.mockRejectedValue(
      new Error("Dashboard authentication required")
    )
    renderPanel([makeScheduler()])

    fireEvent.click(
      screen.getByRole("button", { name: "twelve_data 設為執行中" })
    )
    fireEvent.click(screen.getByRole("button", { name: "確認啟用" }))

    await waitFor(() =>
      expect(mocks.navigate).toHaveBeenCalledWith({
        to: "/login",
        replace: true,
      })
    )
    expect(screen.queryByText("排程狀態更新失敗")).not.toBeInTheDocument()
  })
})

describe("scheduler operations panel", () => {
  it("shows an explicit initial skeleton and preserves data during refresh", () => {
    const { rerender } = render(
      <SchedulerPanel
        result={null}
        loading
        pending
        refreshError=""
        role="owner"
      />
    )

    expect(screen.getByRole("status")).toHaveTextContent("正在載入資料")

    rerender(
      <SchedulerPanel
        result={panelResult([makeScheduler()])}
        loading={false}
        pending
        refreshError=""
        role="owner"
      />
    )
    expect(
      screen.getByText("twelve_data_us_common_stocks_daily_v1")
    ).toBeInTheDocument()
    expect(
      screen.getByText("正在更新排程狀態…目前資料仍可操作。")
    ).toBeInTheDocument()
  })

  it("reserves refresh status space when the scheduler data is not pending", () => {
    const { container, rerender } = render(
      <SchedulerPanel
        result={panelResult([makeScheduler()])}
        loading={false}
        pending={false}
        refreshError=""
        role="owner"
      />
    )

    expect(container.querySelector('[aria-live="polite"]')).toHaveClass(
      "min-h-4"
    )

    rerender(
      <SchedulerPanel
        result={panelResult([makeScheduler()])}
        loading={false}
        pending
        refreshError=""
        role="owner"
      />
    )

    expect(container.querySelector('[aria-live="polite"]')).toHaveClass(
      "min-h-4"
    )
    expect(
      screen.getByText("正在更新排程狀態…目前資料仍可操作。")
    ).toBeInTheDocument()
  })

  it("shows stale heartbeat and read-only affordance for non-owners", () => {
    renderPanel(
      [
        makeScheduler({ heartbeat_age_seconds: null, last_heartbeat_at: null }),
        makeScheduler({
          scheduler_key: "finlab_tw_equity_eod_v1",
          provider: "finlab",
          heartbeat_age_seconds: 91,
        }),
      ],
      { role: "viewer" }
    )

    expect(screen.getAllByText("Heartbeat stale")).toHaveLength(2)
    expect(
      screen.getAllByText("唯讀：只有 owner 可以變更排程狀態。")
    ).toHaveLength(2)
    expect(
      screen.queryByRole("button", { name: /設為/ })
    ).not.toBeInTheDocument()
  })

  it("updates only after owner mutation succeeds and sends the current revision", async () => {
    const response: SchedulerMutationResponse = {
      success: true,
      data: makeScheduler({ desired_state: "running", revision: 2 }),
    }
    mocks.updateScheduler.mockResolvedValue(response)
    const applyScheduler = vi.fn()
    renderPanel([makeScheduler()], { applyScheduler })

    fireEvent.click(
      screen.getByRole("button", { name: "twelve_data 設為執行中" })
    )
    expect(mocks.updateScheduler).not.toHaveBeenCalled()
    expect(screen.getByRole("alertdialog")).toHaveTextContent("確認啟用排程？")
    fireEvent.click(screen.getByRole("button", { name: "確認啟用" }))
    expect(
      screen.getByRole("button", { name: "twelve_data 設為執行中" })
    ).toBeDisabled()
    expect(screen.getByText("更新中…")).toBeInTheDocument()

    await waitFor(() =>
      expect(mocks.updateScheduler).toHaveBeenCalledWith({
        data: {
          schedulerKey: "twelve_data_us_common_stocks_daily_v1",
          desiredState: "running",
          expectedRevision: 1,
        },
      })
    )
    await waitFor(() => expect(applyScheduler).toHaveBeenCalledWith(response))
    expect(await screen.findByRole("status")).toHaveTextContent("排程已啟用")
  })

  it("maps revision conflicts to an actionable error without changing the row", async () => {
    mocks.updateScheduler.mockRejectedValue(
      new Error("Scheduler revision is stale; refresh and retry")
    )
    const applyScheduler = vi.fn()
    renderPanel([makeScheduler()], { applyScheduler })

    fireEvent.click(
      screen.getByRole("button", { name: "twelve_data 設為執行中" })
    )
    fireEvent.click(screen.getByRole("button", { name: "確認啟用" }))

    await waitFor(() =>
      expect(
        screen.getAllByText("排程版本已被其他使用者更新，請先重新整理後再試。")
      ).toHaveLength(2)
    )
    expect(screen.getByText("r1")).toBeInTheDocument()
    expect(applyScheduler).not.toHaveBeenCalled()
    expect(screen.getByText("排程狀態更新失敗")).toBeInTheDocument()
  })

  it("requires confirmation before stopping a scheduler", async () => {
    const response: SchedulerMutationResponse = {
      success: true,
      data: makeScheduler({
        desired_state: "stopped",
        observed_state: "stopped",
        revision: 2,
      }),
    }
    mocks.updateScheduler.mockResolvedValue(response)
    renderPanel([
      makeScheduler({ desired_state: "running", observed_state: "running" }),
    ])

    fireEvent.click(
      screen.getByRole("button", { name: "twelve_data 設為已停止" })
    )
    expect(mocks.updateScheduler).not.toHaveBeenCalled()
    expect(screen.getByRole("alertdialog")).toHaveTextContent("確認停止排程？")

    fireEvent.click(screen.getByRole("button", { name: "確認停止" }))

    await waitFor(() =>
      expect(mocks.updateScheduler).toHaveBeenCalledWith({
        data: {
          schedulerKey: "twelve_data_us_common_stocks_daily_v1",
          desiredState: "stopped",
          expectedRevision: 1,
        },
      })
    )
  })
})

describe("unified ingestion overview", () => {
  function selectRuntimeStatus(
    schedulerOverrides: Partial<Scheduler> = {},
    freshnessOverrides: Partial<MarketFreshness> = {}
  ) {
    return selectSchedulerRuntimeStatus({
      control: makeScheduler(schedulerOverrides),
      freshness: makeFreshness(freshnessOverrides),
    })
  }

  it("selects scheduler runtime status with explicit precedence", () => {
    expect(
      SCHEDULER_RUNTIME_STATUS_META[
        selectRuntimeStatus({
          desired_state: "running",
          observed_state: "stopped",
        })
      ].label
    ).toBe("執行中")
    expect(
      SCHEDULER_RUNTIME_STATUS_META[
        selectRuntimeStatus({
          desired_state: "stopped",
          observed_state: "running",
        })
      ].label
    ).toBe("停止中")
    expect(
      SCHEDULER_RUNTIME_STATUS_META[
        selectRuntimeStatus({
          desired_state: "stopped",
          observed_state: "stopped",
        })
      ].label
    ).toBe("停止")
    expect(
      SCHEDULER_RUNTIME_STATUS_META[
        selectRuntimeStatus({
          heartbeat_age_seconds: null,
          last_heartbeat_at: null,
        })
      ].label
    ).toBe("尚未回報")
    expect(
      SCHEDULER_RUNTIME_STATUS_META[
        selectRuntimeStatus({ heartbeat_age_seconds: 91 })
      ].label
    ).toBe("心跳過期")
    expect(
      SCHEDULER_RUNTIME_STATUS_META[
        selectRuntimeStatus(
          { heartbeat_age_seconds: null, last_heartbeat_at: null },
          { configuration_status: "error" }
        )
      ].label
    ).toBe("設定錯誤")
    expect(
      selectRuntimeStatus(
        {
          desired_state: "running",
          observed_state: "stopped",
          last_error: "provider unavailable",
        },
        { status: "late" }
      )
    ).toBe("running")
  })

  it("keeps scheduler runtime status separate from freshness and recent errors", () => {
    render(
      <IngestionOverviewPanel
        freshnessResult={freshnessResult([
          makeFreshness({
            desired_state: "running",
            observed_state: "stopped",
            status: "late",
            last_error: "provider unavailable",
          }),
        ])}
        schedulersResult={null}
        loading={false}
        pending={false}
        freshnessError=""
        schedulersError=""
        role="viewer"
      />
    )

    expect(screen.getByText("Scheduler 狀態")).toBeInTheDocument()
    expect(screen.getByText("執行中")).toBeInTheDocument()
    expect(screen.getByText("Cycle：閒置")).toBeInTheDocument()
    expect(screen.getByText("最近錯誤：")).toBeInTheDocument()
    expect(screen.getByText("provider unavailable")).toBeInTheDocument()
    expect(screen.queryByText("期望：執行中")).not.toBeInTheDocument()
    expect(screen.queryByText("實際：閒置")).not.toBeInTheDocument()
    expect(screen.queryByText("延遲")).not.toBeInTheDocument()
  })

  it("requires confirmation before changing a scheduler from the overview", () => {
    render(
      <IngestionOverviewPanel
        freshnessResult={freshnessResult([
          makeFreshness({
            desired_state: "stopped",
            observed_state: "stopped",
          }),
        ])}
        schedulersResult={null}
        loading={false}
        pending={false}
        freshnessError=""
        schedulersError=""
        role="owner"
      />
    )

    fireEvent.click(screen.getByRole("button", { name: "finlab 設為執行中" }))

    expect(mocks.updateScheduler).not.toHaveBeenCalled()
    expect(screen.getByRole("alertdialog")).toHaveTextContent("確認啟用排程？")
  })

  it("groups one card per scheduler and keeps schedule/fetch/normalization timestamps visible", () => {
    render(
      <IngestionOverviewPanel
        freshnessResult={freshnessResult([
          makeFreshness(),
          makeFreshness({
            scheduler_key: "finlab_tw_stopped",
            desired_state: "stopped",
            observed_state: "stopped",
            status: "fresh",
          }),
          makeFreshness({
            scheduler_key: "finlab_tw_stale",
            heartbeat_age_seconds: 180,
          }),
          makeFreshness({
            scheduler_key: "finlab_tw_delayed",
            last_fetched_at: "2026-08-04T03:00:00Z",
            last_complete_at: "2026-08-04T02:00:00Z",
          }),
          makeFreshness({
            scheduler_key: "finlab_tw_partial",
            status: "partial",
            last_fetched_at: null,
          }),
          makeFreshness({
            scheduler_key: "finlab_tw_never",
            status: "never_received",
            last_fetched_at: null,
            last_successful_update_at: null,
            last_complete_at: null,
          }),
          makeFreshness({
            scheduler_key: "finlab_tw_config_error",
            configuration_status: "error",
            configuration_errors: ["schema_id_missing"],
          }),
        ])}
        schedulersResult={null}
        loading={false}
        pending={false}
        freshnessError=""
        schedulersError=""
        role="viewer"
      />
    )

    expect(
      screen.getAllByText(
        "每日排程 14:30 · Asia/Taipei · Slot taiwan_market_window"
      )
    ).toHaveLength(7)
    expect(screen.getAllByText("Provider fetched")).toHaveLength(7)
    expect(screen.getAllByText("Normalization completed")).toHaveLength(7)
    expect(screen.getAllByText("已更新").length).toBeGreaterThan(0)
    expect(screen.getByText("停止")).toBeInTheDocument()
    expect(screen.getByText("心跳過期")).toBeInTheDocument()
    expect(screen.getAllByText("執行中").length).toBeGreaterThan(0)
    expect(screen.queryByText("部分完成")).not.toBeInTheDocument()
    expect(screen.queryByText("尚未抓取")).not.toBeInTheDocument()
    expect(screen.queryByText("已抓取，標準化延遲")).not.toBeInTheDocument()
    expect(screen.getByText("設定錯誤")).toBeInTheDocument()
    expect(screen.getByText("schema_id_missing")).toBeInTheDocument()
  })
})

describe("DQ quality provenance", () => {
  it("renders bounded policy provenance and raw reference without raw payload content", () => {
    const issue = qualityIssue()
    render(
      <OperationsContext.Provider value={qualityContextValue(issue)}>
        <QualityPage />
      </OperationsContext.Provider>
    )

    expect(
      screen.getByText(/Provider：finlab · Source：source-api/)
    ).toBeInTheDocument()
    expect(screen.getByText("Dataset：tw_equity_eod")).toBeInTheDocument()
    expect(
      screen.getByText("Run：019565d2-f838-7c91-85c1-72d4d7bbbe98")
    ).toBeInTheDocument()
    expect(screen.getByText("Schema：market_eod.v1")).toBeInTheDocument()
    expect(screen.getByText("批次資料日：2026-08-04")).toBeInTheDocument()
    expect(screen.getByText("Raw：可取得")).toBeInTheDocument()
    expect(
      screen.getByText("Raw payload ID：019565d2-f838-7c91-85c1-72d4d7bbbe99")
    ).toBeInTheDocument()
    expect(screen.getByText(/Fetched：/)).toBeInTheDocument()
    expect(screen.getByText(/code：close_gap/)).toBeInTheDocument()
    expect(screen.getByText(/action：quarantine/)).toBeInTheDocument()
    expect(
      screen.getByText(/reason：provider close differs from expected close/)
    ).toBeInTheDocument()
    expect(screen.getByText(/observed：101/)).toBeInTheDocument()
    expect(screen.getByText(/expected：100/)).toBeInTheDocument()
    expect(screen.getByText(/12 個 policy violation/)).toBeInTheDocument()
    expect(screen.getByText(/truncated／已截斷/)).toBeInTheDocument()
    expect(
      screen.queryByText(/raw-payload-secret-must-not-render/)
    ).not.toBeInTheDocument()
    expect(screen.queryByText(/raw-payload-secret-2/)).not.toBeInTheDocument()
  })
})

describe("view-aware operations loading", () => {
  it("maps basepath and trailing-slash operations URLs without loading governance routes", () => {
    expect(operationsViewForPathname("/dashboard/operations/quality/")).toBe(
      "quality"
    )
    expect(operationsViewForPathname("/dashboard/operations/")).toBe("overview")
    expect(
      operationsViewForPathname("/dashboard/operations/calendars/")
    ).toBeNull()
  })

  const pollingViews = [
    ["overview", "/operations"],
    ["deliveries", "/operations/deliveries"],
    ["quality", "/operations/quality"],
  ] as const

  it.each(pollingViews)(
    "%s polls again after 60 seconds",
    async (view, pathname) => {
      vi.useFakeTimers()
      mocks.pathname = pathname
      render(<OperationsLayout username="operator" role="operator" />)

      expect(mocks.loadDashboard).toHaveBeenCalledTimes(1)
      expect(mocks.loadDashboard).toHaveBeenLastCalledWith({
        data: { view, audit: expect.any(Object) },
      })

      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000)
      })
      expect(mocks.loadDashboard).toHaveBeenCalledTimes(2)
    }
  )

  it.each([
    ["corrections", "/operations/corrections"],
    ["rawPayloads", "/operations/raw-payloads"],
  ] as const)(
    "%s does not install a polling refresh",
    async (view, pathname) => {
      vi.useFakeTimers()
      mocks.pathname = pathname
      render(<OperationsLayout username="operator" role="operator" />)

      expect(mocks.loadDashboard).toHaveBeenCalledTimes(1)
      expect(mocks.loadDashboard).toHaveBeenLastCalledWith({
        data: { view, audit: expect.any(Object) },
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000)
      })
      expect(mocks.loadDashboard).toHaveBeenCalledTimes(1)
    }
  )

  it("does not overlap a same-view refresh while the first request is in flight", async () => {
    vi.useFakeTimers()
    let resolveInitial!: (response: DashboardResponse) => void
    const initial = new Promise<DashboardResponse>(resolve => {
      resolveInitial = resolve
    })
    mocks.loadDashboard.mockImplementation(
      ({ data }: { data: { view: OperationsView } }) => {
        if (data.view === "overview") return initial
        return Promise.resolve(dashboardForView(data.view))
      }
    )

    render(<OperationsLayout username="operator" role="operator" />)
    expect(mocks.loadDashboard).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(mocks.loadDashboard).toHaveBeenCalledTimes(1)

    resolveInitial(dashboardForView("overview"))
    await act(async () => {
      await initial
    })
    expect(mocks.loadDashboard).toHaveBeenCalledTimes(1)
  })

  it("keeps a new route's result when the previous route completes late", async () => {
    vi.useFakeTimers()
    let resolveOverview!: (response: DashboardResponse) => void
    const overviewRequest = new Promise<DashboardResponse>(resolve => {
      resolveOverview = resolve
    })
    mocks.loadDashboard.mockImplementation(
      ({ data }: { data: { view: OperationsView } }) =>
        data.view === "overview"
          ? overviewRequest
          : Promise.resolve(dashboardForView(data.view))
    )

    const { rerender } = render(
      <OperationsLayout username="operator" role="operator" />
    )
    expect(mocks.loadDashboard).toHaveBeenCalledTimes(1)

    mocks.pathname = "/operations/quality"
    rerender(<OperationsLayout username="operator" role="operator" />)
    expect(mocks.loadDashboard).toHaveBeenCalledTimes(2)
    expect(mocks.loadDashboard).toHaveBeenLastCalledWith({
      data: { view: "quality", audit: expect.any(Object) },
    })

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(screen.getByText(/1\/1 個資料來源成功/)).toBeInTheDocument()

    resolveOverview(dashboardForView("overview"))
    await act(async () => {
      await overviewRequest
    })
    expect(screen.getByText(/1\/1 個資料來源成功/)).toBeInTheDocument()
  })
})

describe("raw payload detail retrieval", () => {
  function rawContextValue(
    options: {
      payloads?: RawPayload[]
      totalPages?: number
      setFilters?: OperationsContextValue["setFilters"]
      refresh?: OperationsContextValue["refresh"]
    } = {}
  ): OperationsContextValue {
    return {
      data: rawDashboardData(options.payloads, options.totalPages),
      error: "",
      freshnessError: "",
      schedulersError: "",
      pending: false,
      initialLoading: false,
      role: "viewer",
      applyScheduler: vi.fn(),
      filters: {
        datasetKey: "",
        runId: "",
        dateFrom: "",
        dateTo: "",
        page: 1,
        pageSize: 25,
      },
      setFilters: options.setFilters ?? vi.fn(),
      refresh: options.refresh ?? vi.fn().mockResolvedValue(undefined),
    }
  }

  it("does not fetch before expand, then shows loading/success and caches re-expansion", async () => {
    const detail = makeRawPayload()
    let resolveDetail!: (payload: RawPayload) => void
    const detailRequest = new Promise<RawPayload>(resolve => {
      resolveDetail = resolve
    })
    mocks.loadRawPayloadDetail.mockReturnValue(detailRequest)

    render(
      <OperationsContext.Provider value={rawContextValue()}>
        <RawPayloadsPage />
      </OperationsContext.Provider>
    )

    expect(mocks.loadRawPayloadDetail).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "檢視 JSON" }))
    expect(mocks.loadRawPayloadDetail).toHaveBeenCalledWith({
      data: { rawPayloadId: detail.raw_payload_id },
    })
    expect(screen.getByRole("status")).toHaveTextContent("正在載入完整 JSON…")

    resolveDetail(detail)
    expect(await screen.findByText(/2330/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: "收合 JSON" }))
    fireEvent.click(screen.getByRole("button", { name: "檢視 JSON" }))
    expect(mocks.loadRawPayloadDetail).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(/2330/)).toBeInTheDocument()
  })

  it("sanitizes a missing raw payload detail", async () => {
    mocks.loadRawPayloadDetail.mockRejectedValue(
      new Error("FinDB API request failed (404)")
    )
    render(
      <OperationsContext.Provider value={rawContextValue()}>
        <RawPayloadsPage />
      </OperationsContext.Provider>
    )

    fireEvent.click(screen.getByRole("button", { name: "檢視 JSON" }))
    expect(await screen.findByText("原始資料已不存在")).toBeInTheDocument()
  })

  it("clears cached details when filters or page change", async () => {
    const detail = makeRawPayload()
    const setFilters = vi.fn()
    const refresh = vi.fn().mockResolvedValue(undefined)
    mocks.loadRawPayloadDetail.mockResolvedValue(detail)
    render(
      <OperationsContext.Provider
        value={rawContextValue({
          totalPages: 2,
          setFilters,
          refresh,
        })}
      >
        <RawPayloadsPage />
      </OperationsContext.Provider>
    )

    fireEvent.click(screen.getByRole("button", { name: "檢視 JSON" }))
    expect(await screen.findByText(/2330/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "收合 JSON" }))

    fireEvent.change(screen.getByLabelText("Dataset key"), {
      target: { value: "tw_equity_eod" },
    })
    fireEvent.click(screen.getByRole("button", { name: "查詢" }))
    expect(setFilters).toHaveBeenCalledWith(
      expect.objectContaining({ page: 1 })
    )
    expect(refresh).toHaveBeenCalledWith(expect.objectContaining({ page: 1 }))

    fireEvent.click(screen.getByRole("button", { name: "檢視 JSON" }))
    expect(await screen.findByText(/2330/)).toBeInTheDocument()
    expect(mocks.loadRawPayloadDetail).toHaveBeenCalledTimes(2)
    fireEvent.click(screen.getByRole("button", { name: "收合 JSON" }))

    fireEvent.click(screen.getByRole("button", { name: "第 2 頁" }))
    expect(refresh).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 2 })
    )
    fireEvent.click(screen.getByRole("button", { name: "檢視 JSON" }))
    expect(await screen.findByText(/2330/)).toBeInTheDocument()
    expect(mocks.loadRawPayloadDetail).toHaveBeenCalledTimes(3)
  })
})
