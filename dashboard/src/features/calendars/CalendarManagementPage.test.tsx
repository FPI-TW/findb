import { DashboardAuthenticationError } from "../../lib/auth-errors"
import { OperationsRefreshProvider } from "../../components/OperationsRefresh"
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

const mocks = vi.hoisted(() => ({
  loadCalendarMarkets: vi.fn(),
  loadCalendarYear: vi.fn(),
  loadCalendarImports: vi.fn(),
  loadCalendarRevisions: vi.fn(),
  applyCalendarPreview: vi.fn(),
  editCalendarDay: vi.fn(),
  previewCalendarCsv: vi.fn(),
  previewCalendarJson: vi.fn(),
  publishCalendarYear: vi.fn(),
  rollbackCalendarYear: vi.fn(),
}))

vi.mock("@tanstack/react-start", () => ({
  useServerFn: (serverFn: unknown) => serverFn,
}))
vi.mock("../../lib/calendar.functions", () => mocks)

import { chooseSelectOption } from "../../test-utils/select"
import { CalendarManagementPage } from "./CalendarManagementPage"
import type { CalendarSearch } from "./calendar.search"

const search: CalendarSearch = {
  market: "TW",
  year: 2026,
  month: "",
  status: "",
  q: "",
}

function renderPage(
  role: "owner" | "operator" | "viewer" = "viewer",
  updateSearch = vi.fn()
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <CalendarManagementPage
        role={role}
        search={search}
        updateSearch={updateSearch}
      />
    </QueryClientProvider>
  )
  return { queryClient, updateSearch }
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.loadCalendarMarkets.mockResolvedValue([
    {
      market: "TW",
      display_name: "台灣",
      timezone: "Asia/Taipei",
      weekend_days: [5, 6],
      active: true,
      default_session_open: "09:00",
      default_session_close: "13:30",
    },
  ])
  mocks.loadCalendarYear.mockResolvedValue({
    market: "TW",
    year: 2026,
    timezone: "Asia/Taipei",
    draft_revision: null,
    published_revision: null,
    effective_days: [
      {
        trade_date: "2026-01-02",
        is_open: true,
        calendar_source: "weekday_default",
        calendar_revision: null,
        regular_close: "13:30:00",
      },
    ],
    current_revision: 0,
    coverage_complete: false,
    summary: {
      total: 1,
      open: 1,
      closed: 0,
      settlement_only: 0,
      warnings: 0,
      missing: 0,
      duplicates: 0,
    },
    days: [
      {
        date: "2026-01-02",
        status: "open",
        is_open: true,
        name: "開市日",
        description: null,
        session_open: "09:00",
        session_close: "13:30",
        source_kind: "market_default",
      },
    ],
  })
  mocks.loadCalendarImports.mockResolvedValue([])
  mocks.loadCalendarRevisions.mockResolvedValue([])
})

afterEach(() => cleanup())

describe("CalendarManagementPage", () => {
  it("renders the yearly DataTable and synchronizes filters to route search", async () => {
    const { updateSearch } = renderPage()

    expect(
      await screen.findByRole("table", { name: "全年交易日曆" })
    ).toBeInTheDocument()
    expect(screen.getByText("2026-01-02")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "編輯 2026-01-02" })).toBeNull()

    chooseSelectOption(screen.getByLabelText("月份篩選"), "1 月")
    expect(updateSearch).toHaveBeenCalledWith({ ...search, month: "01" })
  })

  it("applies the market and year together only when 查詢 is submitted", async () => {
    mocks.loadCalendarMarkets.mockResolvedValue([
      {
        market: "TW",
        display_name: "台灣",
        timezone: "Asia/Taipei",
        weekend_days: [5, 6],
        active: true,
        default_session_open: "09:00",
        default_session_close: "13:30",
      },
      {
        market: "HK",
        display_name: "香港",
        timezone: "Asia/Hong_Kong",
        weekend_days: [5, 6],
        active: true,
        default_session_open: "09:30",
        default_session_close: "16:00",
      },
    ])
    const { updateSearch } = renderPage()
    await screen.findByRole("table", { name: "全年交易日曆" })
    const submit = screen.getByRole("button", { name: "查詢" })
    expect(submit).toBeDisabled()

    chooseSelectOption(screen.getByLabelText("市場"), "香港(HK)")
    const year = screen.getByLabelText("年度")
    // Each keystroke of a year is only a draft: nothing loads until 查詢.
    for (const value of ["2", "20", "202", "2025"]) {
      fireEvent.change(year, { target: { value } })
    }
    expect(updateSearch).not.toHaveBeenCalled()
    expect(mocks.loadCalendarYear).toHaveBeenCalledTimes(1)

    fireEvent.click(submit)
    expect(updateSearch).toHaveBeenCalledTimes(1)
    expect(updateSearch).toHaveBeenCalledWith({
      ...search,
      market: "HK",
      year: 2025,
    })
  })

  it("refuses an out-of-range year and submits a valid one with Enter", async () => {
    const { updateSearch } = renderPage()
    await screen.findByRole("table", { name: "全年交易日曆" })
    const year = screen.getByLabelText("年度")

    fireEvent.change(year, { target: { value: "1899" } })
    expect(screen.getByRole("button", { name: "查詢" })).toBeDisabled()
    expect(year).toHaveAttribute("aria-invalid", "true")
    expect(screen.getByText("年度需為 1900–2200 的四位數")).toBeInTheDocument()

    fireEvent.change(year, { target: { value: "2027" } })
    expect(year).toHaveAttribute("aria-invalid", "false")
    fireEvent.submit(screen.getByRole("form", { name: "選擇市場與年度" }))
    expect(updateSearch).toHaveBeenCalledWith({ ...search, year: 2027 })
  })

  it("pins an explicit edit action for permitted roles", async () => {
    renderPage("operator")

    expect(
      await screen.findByRole("button", { name: "編輯 2026-01-02" })
    ).toBeInTheDocument()
  })

  it("shows a contextual error instead of an empty calendar", async () => {
    mocks.loadCalendarYear.mockRejectedValue(new Error("calendar unavailable"))
    renderPage()

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "calendar unavailable"
    )
    expect(screen.queryByRole("table", { name: "全年交易日曆" })).toBeNull()
  })

  it("loads history only when visible and distinguishes a failed source from an empty source", async () => {
    mocks.loadCalendarImports.mockRejectedValue(
      new Error("imports unavailable")
    )
    renderPage()
    await screen.findByText("2026-01-02")
    expect(mocks.loadCalendarImports).not.toHaveBeenCalled()
    expect(mocks.loadCalendarRevisions).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole("button", { name: "修訂／匯入紀錄" }))
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "imports unavailable"
    )
    expect(screen.getByText("此市場年度尚無修訂紀錄。")).toBeInTheDocument()
    expect(screen.queryByText("此市場年度尚無匯入紀錄。")).toBeNull()
  })

  it("keeps a successful empty history visible while refreshing and after a refresh failure", async () => {
    const { queryClient } = renderPage()
    await screen.findByText("2026-01-02")
    fireEvent.click(screen.getByRole("button", { name: "修訂／匯入紀錄" }))
    await screen.findByText("此市場年度尚無匯入紀錄。")
    let rejectRefresh!: (error: Error) => void
    mocks.loadCalendarImports.mockImplementation(
      () =>
        new Promise((_, reject) => {
          rejectRefresh = reject
        })
    )
    let refresh!: Promise<void>
    act(() => {
      refresh = queryClient.refetchQueries({ type: "active" })
    })
    await waitFor(() => expect(rejectRefresh).toBeDefined())
    expect(screen.getByText("此市場年度尚無匯入紀錄。")).toBeInTheDocument()
    await act(async () => {
      rejectRefresh(new Error("imports refresh failed"))
      await refresh
    })
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "保留最後成功資料"
    )
    expect(screen.getByText("此市場年度尚無匯入紀錄。")).toBeInTheDocument()
  })
})

it("calendar direct edit expiry hides the editor and removes its protected data", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const navigate = vi.fn()
  mocks.editCalendarDay.mockRejectedValue(new DashboardAuthenticationError())
  render(
    <QueryClientProvider client={client}>
      <OperationsRefreshProvider onAuthenticationFailure={navigate}>
        <CalendarManagementPage
          role="operator"
          search={search}
          updateSearch={() => {}}
        />
      </OperationsRefreshProvider>
    </QueryClientProvider>
  )
  fireEvent.click(
    await screen.findByRole("button", { name: "編輯 2026-01-02" })
  )
  fireEvent.change(screen.getByLabelText("修改理由"), {
    target: { value: "fixture edit" },
  })
  fireEvent.click(screen.getByRole("button", { name: "儲存草稿" }))
  await screen.findByText("登入已失效，正在返回登入頁…")
  expect(screen.queryByText("2026-01-02")).toBeNull()
  expect(client.getQueryCache().findAll()).toHaveLength(0)
  expect(navigate).toHaveBeenCalledTimes(1)
})

it("labels effective defaults without presenting a published revision", async () => {
  renderPage()
  expect(await screen.findByText("週一至週五預設 · 交易日")).toBeInTheDocument()
  expect(screen.queryByText(/已發布日曆 r0/)).not.toBeInTheDocument()
})
