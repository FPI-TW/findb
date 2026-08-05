import "@testing-library/jest-dom/vitest"

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { toast, Toaster } from "../../components/ui/toast"
import type {
  Scheduler,
  SchedulerMutationResponse,
  SchedulersResponse,
} from "../../lib/admin-api"

const mocks = vi.hoisted(() => ({
  loadDashboard: vi.fn(),
  updateScheduler: vi.fn(),
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

import { SchedulerPanel } from "./OperationsConsole"

const timestamp = "2026-08-04T02:00:00Z"

function makeScheduler(overrides: Partial<Scheduler> = {}): Scheduler {
  return {
    scheduler_key: "twelve_data_us_common_stocks_daily_v1",
    provider: "twelve_data",
    dataset_keys: ["us_equity_eod"],
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
})

afterEach(() => {
  toast.dismiss()
  cleanup()
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

  it("shows stale heartbeat and read-only affordance for non-owners", () => {
    renderPanel(
      [
        makeScheduler({ heartbeat_age_seconds: null, last_heartbeat_at: null }),
        makeScheduler({
          scheduler_key: "finlab_tw_1430_tw_equity_eod",
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

    await waitFor(() =>
      expect(
        screen.getAllByText("排程版本已被其他使用者更新，請先重新整理後再試。")
      ).toHaveLength(2)
    )
    expect(screen.getByText("r1")).toBeInTheDocument()
    expect(applyScheduler).not.toHaveBeenCalled()
    expect(screen.getByText("排程狀態更新失敗")).toBeInTheDocument()
  })
})
