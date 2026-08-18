import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
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

    fireEvent.change(screen.getByLabelText("月份篩選"), {
      target: { value: "01" },
    })
    expect(updateSearch).toHaveBeenCalledWith({ ...search, month: "01" })
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
})
