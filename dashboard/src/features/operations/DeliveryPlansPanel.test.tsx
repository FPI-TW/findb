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
    gaps: [
      { member_key: "TX", status: "missing", reason: "pending" },
      { member_key: "MTX", status: "blocked", reason: "rate_limited" },
    ],
  },
}
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
    expect(screen.getByText("正在載入全市場交付計畫…")).toBeInTheDocument()
    expect(screen.queryByText("此條件沒有交付計畫。")).not.toBeInTheDocument()
    await screen.findByRole("option", { name: "old_dataset" })
    await act(async () => resolve({ data: [plan], pagination }))
    expect(await screen.findByText("已逾時")).toBeInTheDocument()
    expect(
      screen.getByRole("columnheader", { name: "正常無資料" })
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "展開詳細資料" }))
    expect(screen.getByText("TX")).toBeInTheDocument()
    expect(
      screen.getByText(/來源請求頻率受限（rate_limited）/)
    ).toBeInTheDocument()
    expect(screen.getByText(/已列出 2／總缺口 2/)).toBeInTheDocument()
  })
  it("retains expanded rows and prior results after refetch failure, then recovers", async () => {
    const { client } = renderPanel()
    fireEvent.click(await screen.findByRole("button", { name: "展開詳細資料" }))
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
    await screen.findByText("此條件沒有交付計畫。")
    mocks.loadDeliveryPlans.mockRejectedValue(new Error("offline"))
    await act(() =>
      client.refetchQueries({ queryKey: ["operations", "delivery-plans"] })
    )
    expect(screen.getByText("此條件沒有交付計畫。")).toBeInTheDocument()
    expect(await screen.findByText(/保留最後成功資料/)).toBeInTheDocument()
  })
  it("uses server pagination and dataset/date filters without showing previous-filter rows", async () => {
    mocks.loadDeliveryPlans.mockResolvedValue({
      data: [plan],
      pagination: { ...pagination, total_records: 101, total_pages: 5 },
    })
    renderPanel()
    await screen.findByRole("button", { name: "展開詳細資料" })
    fireEvent.click(
      screen.getByRole("button", { name: "全市場計畫分頁下一頁" })
    )
    await waitFor(() =>
      expect(mocks.loadDeliveryPlans).toHaveBeenCalledWith({
        data: { datasetKey: "", tradeDate: "", page: 2, pageSize: 25 },
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
          summary: {
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
          },
        },
      ],
      pagination,
    })
    renderPanel()
    fireEvent.click(await screen.findByRole("button", { name: "展開詳細資料" }))
    expect(screen.getByText(/已列出 1／總缺口 1502/)).toBeInTheDocument()
    expect(screen.getByText(/new_provider_reason/)).toBeInTheDocument()
  })
})
