import "@testing-library/jest-dom/vitest"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react"
import type { ReactNode } from "react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { completenessSchema } from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import { chooseSelectOption } from "../../test-utils/select"
import { CompletenessPage } from "./operations.completeness"
import {
  completenessSearchSchema,
  type CompletenessSearch,
} from "./operations.search"

const mocks = vi.hoisted(() => ({
  loadCompleteness: vi.fn(),
  loadDeliveryPlanSummary: vi.fn(),
}))
vi.mock("../../lib/admin.functions", () => mocks)
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
vi.mock("@tanstack/react-router", () => ({
  useHydrated: () => true,
  Link: ({
    children,
    search,
  }: {
    children: ReactNode
    search?: Record<string, string>
  }) => <a data-search={JSON.stringify(search ?? {})}>{children}</a>,
}))

const PLAN = "0195a000-0000-7000-8000-000000000001"
const counts = (missing: number, blocked = 0) => ({
  expected: 10,
  data: 10 - missing - blocked,
  no_data: 0,
  missing,
  blocked,
  unavailable: 0,
  mapping_gap: 0,
  unlisted_data: 0,
})
const cell = (trade_date: string, state: string, extra = {}) => ({
  trade_date,
  state,
  plan_id: null,
  deadline_at: null,
  counts: null,
  counts_synced: true,
  open_alert: false,
  ...extra,
})
const matrix = completenessSchema.parse({
  execution_profile: "full_market",
  date_from: "2026-11-21",
  date_to: "2026-11-24",
  evaluated_at: "2026-11-24T12:00:00Z",
  rows: [
    {
      dataset_key: "tw_equity_eod",
      provider: "finlab",
      market: "TW",
      first_start_date: "2026-11-01",
      latest_due_date: "2026-11-24",
      cells: [
        cell("2026-11-21", "complete", { plan_id: PLAN, counts: counts(0) }),
        cell("2026-11-22", "closed"),
        cell("2026-11-23", "closed"),
        cell("2026-11-24", "late", {
          plan_id: PLAN,
          counts: counts(3, 1),
          deadline_at: "2026-11-24T10:00:00Z",
          open_alert: true,
          counts_synced: false,
        }),
      ],
    },
    {
      dataset_key: "us_index_eod",
      provider: "fmp",
      market: "US",
      first_start_date: "2026-11-21",
      latest_due_date: "2026-11-23",
      cells: [
        cell("2026-11-21", "complete", { plan_id: PLAN, counts: counts(0) }),
        cell("2026-11-22", "closed"),
        cell("2026-11-23", "closed"),
        cell("2026-11-24", "not_due"),
      ],
    },
  ],
})

function renderPage(
  search: Partial<CompletenessSearch> = {},
  role: AdminRole = "operator"
) {
  const updateSearch = vi.fn()
  render(
    <QueryClientProvider client={new QueryClient()}>
      <CompletenessPage
        search={completenessSearchSchema.parse(search)}
        updateSearch={updateSearch}
        role={role}
      />
    </QueryClientProvider>
  )
  return updateSearch
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.loadCompleteness.mockResolvedValue(matrix)
  mocks.loadDeliveryPlanSummary.mockResolvedValue({
    expected: 10,
    data: 6,
    no_data: 0,
    missing: 3,
    blocked: 1,
    unavailable: 0,
    mapping_gap: 0,
    deadline_at: "2026-11-24T10:00:00Z",
    is_late: true,
    gaps: [{ member_key: "2330", status: "missing", reason: "pending" }],
    unlisted_data: 0,
    unlisted: [],
    gaps_total: 4,
  })
})
afterEach(cleanup)

describe("completeness matrix", () => {
  it("shows a skeleton before the first response, never an empty state", () => {
    mocks.loadCompleteness.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByText("正在載入完整性矩陣")).toBeInTheDocument()
    expect(screen.queryByText("尚無全市場資料集。")).not.toBeInTheDocument()
  })

  it("renders every feed by trade date with states and gap counts", async () => {
    renderPage()
    const table = await screen.findByRole("table", { name: "完整性矩陣" })
    expect(mocks.loadCompleteness).toHaveBeenCalledWith({
      data: { dateFrom: "", dateTo: "", datasetKey: "" },
    })
    expect(within(table).getAllByRole("columnheader")).toHaveLength(5)
    const late = within(table).getByRole("button", {
      name: "tw_equity_eod finlab 2026-11-24：逾時，缺口 4，計數同步中，有未關閉告警",
    })
    expect(late).toHaveTextContent("◐4")
    expect(
      within(table).getByRole("button", {
        name: "us_index_eod fmp 2026-11-24：尚未到期",
      })
    ).toHaveTextContent("○")
  })

  it("selects a cell through the URL search", async () => {
    const updateSearch = renderPage()
    const table = await screen.findByRole("table", { name: "完整性矩陣" })
    fireEvent.click(
      within(table).getByRole("button", {
        name: /tw_equity_eod finlab 2026-11-21/,
      })
    )
    expect(updateSearch).toHaveBeenCalledWith(
      expect.objectContaining({
        dataset: "tw_equity_eod",
        provider: "finlab",
        date: "2026-11-21",
      })
    )
  })

  it("filters rows by market and by having gaps, client side", async () => {
    const updateSearch = renderPage({ gaps: true })
    const table = await screen.findByRole("table", { name: "完整性矩陣" })
    expect(within(table).getByText("tw_equity_eod")).toBeInTheDocument()
    expect(within(table).queryByText("us_index_eod")).not.toBeInTheDocument()
    chooseSelectOption(screen.getByRole("combobox", { name: "市場" }), "US")
    expect(updateSearch).toHaveBeenCalledWith(
      expect.objectContaining({ market: "US", gaps: true })
    )
    expect(mocks.loadCompleteness).toHaveBeenCalledTimes(1)
  })

  it("refuses a range over 62 days without asking the server", () => {
    renderPage({ from: "2026-01-01", to: "2026-03-31" })
    expect(screen.getByRole("alert")).toHaveTextContent("日期範圍最多 62 天。")
    expect(mocks.loadCompleteness).not.toHaveBeenCalled()
  })

  it("details the selected cell and loads its gaps only when asked", async () => {
    renderPage({
      dataset: "tw_equity_eod",
      provider: "finlab",
      date: "2026-11-24",
    })
    const details = await screen.findByRole("region", { name: "所選日期明細" })
    expect(details).toHaveTextContent("逾時")
    expect(details).toHaveTextContent("計數同步中")
    expect(details).toHaveTextContent("有未關閉告警")
    expect(within(details).getByText("缺漏").nextSibling).toHaveTextContent("3")
    expect(mocks.loadDeliveryPlanSummary).not.toHaveBeenCalled()

    fireEvent.click(
      within(details).getByRole("button", { name: "載入缺口明細" })
    )
    await within(details).findByText("2330")
    expect(mocks.loadDeliveryPlanSummary).toHaveBeenCalledWith({
      data: { planId: PLAN, gapStatus: [], gapPage: 1, gapPageSize: 200 },
    })
    expect(details).toHaveTextContent("共 4 筆")

    chooseSelectOption(
      within(details).getByRole("combobox", { name: "缺口狀態" }),
      "受阻"
    )
    await waitFor(() =>
      expect(mocks.loadDeliveryPlanSummary).toHaveBeenLastCalledWith({
        data: {
          planId: PLAN,
          gapStatus: ["blocked"],
          gapPage: 1,
          gapPageSize: 200,
        },
      })
    )
    const backfill = within(details).getByText("帶入回補")
    expect(JSON.parse(backfill.getAttribute("data-search") ?? "{}")).toEqual({
      tab: "backfills",
      seed: "finlab|tw_equity_eod|2026-11-24",
    })
  })

  it("offers no backfill to a viewer", async () => {
    renderPage(
      { dataset: "tw_equity_eod", provider: "finlab", date: "2026-11-24" },
      "viewer"
    )
    const details = await screen.findByRole("region", { name: "所選日期明細" })
    expect(within(details).queryByText("帶入回補")).not.toBeInTheDocument()
    expect(within(details).getByText("查看缺漏告警")).toBeInTheDocument()
  })
})
