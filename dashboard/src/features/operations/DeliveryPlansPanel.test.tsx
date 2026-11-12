import "@testing-library/jest-dom/vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { useState } from "react"
import { deliveriesSearchSchema } from "./operations.search"

const mocks = vi.hoisted(() => ({
  loadDeliveryPlans: vi.fn(),
  loadDeliveryPlanDatasets: vi.fn(),
  loadDeliveryPlanSummary: vi.fn(),
}))
vi.mock("@tanstack/react-start", async importOriginal => ({
  ...(await importOriginal<typeof import("@tanstack/react-start")>()),
  useServerFn: (fn: unknown) => fn,
}))
vi.mock("../../lib/admin.functions", () => mocks)
import { DeliveryPlansPanel } from "./DeliveryPlansPanel"

const plan = {
  plan_id: "p1",
  dataset_key: "tw_futures_eod",
  provider: "taifex",
  trade_date: "2026-10-01",
  release_id: "r1",
  deadline_at: "2026-10-01T15:00:00Z",
  status: "incomplete",
  parts: [],
  summary: {
    expected: 5,
    data: 2,
    no_data: 1,
    missing: 1,
    blocked: 1,
    deadline_at: "2026-10-01T15:00:00Z",
    is_late: true,
    // The list carries counts only; details come from the per-plan summary.
    gaps: [],
  },
}
const gaps = [
  { member_key: "TX", status: "missing", reason: "pending" },
  { member_key: "MTX", status: "blocked", reason: "rate_limited" },
]
const pagination = { page: 1, page_size: 25, total_records: 1, total_pages: 1 }
function PanelHarness({ active }: { active: boolean }) {
  const [search, updateSearch] = useState(() =>
    deliveriesSearchSchema.parse({})
  )
  return (
    <DeliveryPlansPanel
      search={search}
      updateSearch={updateSearch}
      active={active}
    />
  )
}
function renderPanel(active = true) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const view = render(
    <QueryClientProvider client={client}>
      <PanelHarness active={active} />
    </QueryClientProvider>
  )
  return { client, ...view }
}
beforeEach(() => {
  mocks.loadDeliveryPlanDatasets.mockResolvedValue({
    data: ["old_dataset", "tw_futures_eod"],
  })
  mocks.loadDeliveryPlans.mockResolvedValue({ data: [plan], pagination })
  mocks.loadDeliveryPlanSummary.mockResolvedValue({ ...plan.summary, gaps })
})
afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe("full market plans", () => {
  it("shows initial skeleton, complete dataset options, and expands evidence without conflating no-data with failure", async () => {
    let resolve!: (data: unknown) => void
    mocks.loadDeliveryPlans.mockReturnValue(
      new Promise(value => {
        resolve = value
      })
    )
    renderPanel()
    expect(screen.getByText("正在載入交付計畫…")).toBeInTheDocument()
    expect(
      screen.queryByText("所選條件尚無交付計畫；不代表已完成或已有資料缺漏。")
    ).not.toBeInTheDocument()
    await screen.findByRole("option", { name: "old_dataset" })
    await act(async () => resolve({ data: [plan], pagination }))
    expect(await screen.findByText("已逾時")).toBeInTheDocument()
    expect(
      screen.getByRole("columnheader", { name: "正常無資料" })
    ).toBeInTheDocument()
    // Nothing is asked about a plan's members until its row is opened.
    expect(mocks.loadDeliveryPlanSummary).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "展開詳細資料" }))
    expect(await screen.findByText("TX")).toBeInTheDocument()
    expect(mocks.loadDeliveryPlanSummary).toHaveBeenCalledWith({
      data: { planId: "p1" },
    })
    expect(
      screen.getByText(/來源請求頻率受限（rate_limited）/)
    ).toBeInTheDocument()
    expect(screen.getByText(/已列出 2／總缺口 2/)).toBeInTheDocument()
  })
  it("shows the gap total while details load, and keeps it when they fail", async () => {
    let resolve!: (data: unknown) => void
    mocks.loadDeliveryPlanSummary.mockReturnValue(
      new Promise(value => {
        resolve = value
      })
    )
    const { client } = renderPanel()
    fireEvent.click(await screen.findByRole("button", { name: "展開詳細資料" }))
    expect(await screen.findByText("正在載入缺口明細…")).toBeInTheDocument()
    expect(screen.getByText(/總缺口 2/)).toBeInTheDocument()
    expect(screen.queryByText(/已列出/)).not.toBeInTheDocument()
    expect(screen.queryByText("此計畫沒有待處理缺口。")).not.toBeInTheDocument()
    await act(async () => resolve({ ...plan.summary, gaps }))
    expect(await screen.findByText("TX")).toBeInTheDocument()
    mocks.loadDeliveryPlanSummary.mockRejectedValue(new Error("HTTP 503"))
    await act(() =>
      client.refetchQueries({
        queryKey: ["operations", "delivery-plan-summary"],
      })
    )
    expect(
      await screen.findByText(/缺口明細更新失敗，保留最後成功資料/)
    ).toBeInTheDocument()
    expect(screen.getByText("TX")).toBeInTheDocument()
  })
  it("does not ask for details of a plan without gaps", async () => {
    mocks.loadDeliveryPlans.mockResolvedValue({
      data: [
        {
          ...plan,
          status: "complete",
          summary: { ...plan.summary, data: 4, missing: 0, blocked: 0 },
        },
      ],
      pagination,
    })
    renderPanel()
    fireEvent.click(await screen.findByRole("button", { name: "展開詳細資料" }))
    expect(
      await screen.findByText("此計畫沒有待處理缺口。")
    ).toBeInTheDocument()
    expect(mocks.loadDeliveryPlanSummary).not.toHaveBeenCalled()
  })
  it("retains expanded rows and prior results after refetch failure, then recovers", async () => {
    const { client } = renderPanel()
    fireEvent.click(await screen.findByRole("button", { name: "展開詳細資料" }))
    expect(await screen.findByText("TX")).toBeInTheDocument()
    mocks.loadDeliveryPlans.mockRejectedValue(new Error("HTTP 503"))
    await act(() =>
      client.refetchQueries({ queryKey: ["operations", "delivery-plans"] })
    )
    expect(screen.getByText("TX")).toBeInTheDocument()
    expect(await screen.findByText(/保留最後成功資料/)).toBeInTheDocument()
    mocks.loadDeliveryPlans.mockResolvedValue({ data: [plan], pagination })
    await act(() =>
      client.refetchQueries({ queryKey: ["operations", "delivery-plans"] })
    )
    await waitFor(() =>
      expect(screen.queryByText(/HTTP 503/)).not.toBeInTheDocument()
    )
    expect(screen.getByText("TX")).toBeInTheDocument()
  })
  it("retains successful empty results after failure and never calls hidden queries", async () => {
    mocks.loadDeliveryPlans.mockResolvedValue({
      data: [],
      pagination: { ...pagination, total_records: 0, total_pages: 0 },
    })
    const hidden = renderPanel(false)
    expect(mocks.loadDeliveryPlans).not.toHaveBeenCalled()
    expect(mocks.loadDeliveryPlanDatasets).not.toHaveBeenCalled()
    hidden.unmount()
    const { client } = renderPanel()
    await screen.findByText(
      "所選條件尚無交付計畫；不代表已完成或已有資料缺漏。"
    )
    mocks.loadDeliveryPlans.mockRejectedValue(new Error("offline"))
    await act(() =>
      client.refetchQueries({ queryKey: ["operations", "delivery-plans"] })
    )
    expect(
      screen.getByText("所選條件尚無交付計畫；不代表已完成或已有資料缺漏。")
    ).toBeInTheDocument()
    expect(await screen.findByText(/保留最後成功資料/)).toBeInTheDocument()
  })
  it("uses server pagination and dataset/date filters without showing previous-filter rows", async () => {
    mocks.loadDeliveryPlans.mockResolvedValue({
      data: [plan],
      pagination: { ...pagination, total_records: 101, total_pages: 5 },
    })
    renderPanel()
    await screen.findByRole("button", { name: "展開詳細資料" })
    fireEvent.click(screen.getByRole("button", { name: "交付計畫分頁下一頁" }))
    await waitFor(() =>
      expect(mocks.loadDeliveryPlans).toHaveBeenCalledWith({
        data: {
          datasetKey: "",
          tradeDate: "",
          page: 2,
          pageSize: 25,
        },
      })
    )
    mocks.loadDeliveryPlans.mockReturnValue(new Promise(() => {}))
    fireEvent.change(screen.getByLabelText("資料集"), {
      target: { value: "old_dataset" },
    })
    await waitFor(() =>
      expect(mocks.loadDeliveryPlans).toHaveBeenCalledWith({
        data: {
          datasetKey: "old_dataset",
          tradeDate: "",
          page: 1,
          pageSize: 25,
        },
      })
    )
    expect(screen.queryByText("已逾時")).not.toBeInTheDocument()
  })
  it("shows the total gap count even when details are capped and retains unknown reason codes", async () => {
    mocks.loadDeliveryPlans.mockResolvedValue({
      data: [
        {
          ...plan,
          summary: { ...plan.summary, expected: 1505, missing: 1501 },
        },
      ],
      pagination,
    })
    mocks.loadDeliveryPlanSummary.mockResolvedValue({
      ...plan.summary,
      expected: 1505,
      missing: 1501,
      gaps: [
        {
          member_key: "TX",
          status: "blocked",
          reason: "new_provider_reason",
        },
      ],
    })
    renderPanel()
    fireEvent.click(await screen.findByRole("button", { name: "展開詳細資料" }))
    expect(await screen.findByText(/已列出 1／總缺口 1502/)).toBeInTheDocument()
    expect(screen.getByText(/new_provider_reason/)).toBeInTheDocument()
  })
})

it("always requests the daily delivery plans without a profile selector", async () => {
  mocks.loadDeliveryPlans.mockResolvedValue({ data: [plan], pagination })
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <DeliveryPlansPanel
        search={deliveriesSearchSchema.parse({ profile: "pilot" })}
      />
    </QueryClientProvider>
  )
  await screen.findByText("已逾時")
  expect(mocks.loadDeliveryPlans).toHaveBeenCalledWith({
    data: expect.not.objectContaining({ profile: expect.anything() }),
  })
  expect(mocks.loadDeliveryPlanDatasets).toHaveBeenCalledWith()
  expect(screen.queryByText(/Pilot/)).not.toBeInTheDocument()
})

it("shows durable policy skips separately from plan totals", async () => {
  mocks.loadDeliveryPlans.mockResolvedValue({
    data: [plan],
    pagination,
    execution_policies: { finlab: { skip_non_trading_days: true } },
    skipped_total: 1,
    skipped: [
      {
        execution_profile: "full_market",
        dataset_key: "tw_equity_eod",
        provider: "finlab",
        trade_date: "2026-10-03",
        status: "skipped_calendar",
        snapshot: {
          calendar_source: "weekday_default",
          calendar_revision: null,
        },
      },
    ],
  })
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <DeliveryPlansPanel search={deliveriesSearchSchema.parse({})} />
    </QueryClientProvider>
  )
  expect(await screen.findByText(/2026-10-03 · finlab/)).toBeInTheDocument()
  expect(screen.getByText(/依設定略過（1 個日期／資料集）/)).toBeInTheDocument()
  expect(screen.getByText(/finlab：略過/)).toBeInTheDocument()
})
