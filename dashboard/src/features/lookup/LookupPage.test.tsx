import "@testing-library/jest-dom/vitest"

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { ReactNode } from "react"

import { DEFAULT_SEARCH } from "./config"

const mocks = vi.hoisted(() => ({
  loadAllLookupItems: vi.fn(),
  loadLookupPage: vi.fn(),
  loadCorporateActions: vi.fn(),
  loadMacroObservations: vi.fn(),
  loadPrices: vi.fn(),
}))

vi.mock("./data", () => mocks)

import { LookupPage } from "./LookupPage"

const response = {
  success: true as const,
  data: [
    {
      instrument_id: "instrument-1",
      market: "TW",
      asset_class: "equity",
      symbol: "2330",
      name: "台積電",
      currency: "TWD",
      status: "active",
      first_trade_date: "1994-09-05",
      latest_trade_date: "2026-07-22",
      latest_price: "1085.5",
    },
  ],
  pagination: {
    page: 1,
    page_size: 50,
    total_records: 101,
    total_pages: 3,
    next_cursor: null,
  },
  facets: {
    markets: ["TW"],
    asset_classes: ["equity"],
    statuses: ["active"],
  },
}

function renderWithQuery(ui: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>
  )
}

beforeEach(() => {
  localStorage.clear()
  mocks.loadLookupPage.mockReset().mockResolvedValue(response)
  mocks.loadAllLookupItems.mockReset().mockResolvedValue(response.data)
  mocks.loadPrices.mockReset().mockResolvedValue([])
  mocks.loadCorporateActions.mockReset().mockResolvedValue([])
  mocks.loadMacroObservations.mockReset().mockResolvedValue([])
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: vi.fn().mockResolvedValue(undefined) },
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("LookupPage", () => {
  it("uses a pinned detail action and keeps identifier copy separate", async () => {
    const updateSearch = vi.fn()
    renderWithQuery(
      <LookupPage search={DEFAULT_SEARCH} updateSearch={updateSearch} />
    )

    await screen.findByText("台積電")
    expect(
      screen.getByRole("button", { name: "查看 2330 詳情" })
    ).toBeInTheDocument()
    const detailHeader = screen.getByRole("columnheader", { name: /詳細/ })
    expect(detailHeader.style.right).toBe("0px")

    const nameCell = screen.getByRole("cell", { name: "台積電" })
    expect(nameCell.querySelectorAll("button")).toHaveLength(0)

    fireEvent.click(screen.getByTitle("複製 2330"))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("2330")
    expect(updateSearch).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole("button", { name: "查看 2330 詳情" }))
    expect(updateSearch).toHaveBeenCalledWith({
      ...DEFAULT_SEARCH,
      id: "instrument-1",
    })
  })

  it("maps DataTable sorting and pagination back to the existing API search", async () => {
    const updateSearch = vi.fn()
    renderWithQuery(
      <LookupPage search={DEFAULT_SEARCH} updateSearch={updateSearch} />
    )

    await screen.findByText("台積電")
    fireEvent.click(screen.getByRole("button", { name: /依Name排序/ }))
    expect(updateSearch).toHaveBeenCalledWith({
      ...DEFAULT_SEARCH,
      sb: "name",
      sd: "asc",
      p: 1,
      id: "",
    })

    updateSearch.mockClear()
    fireEvent.click(screen.getByRole("button", { name: "下一頁" }))
    expect(updateSearch).toHaveBeenCalledWith({
      ...DEFAULT_SEARCH,
      p: 2,
      id: "",
    })
  })

  it("keeps the initial skeleton and reports a query error without rendering empty state", async () => {
    let rejectRequest: ((reason: Error) => void) | undefined
    mocks.loadLookupPage.mockImplementation(
      () =>
        new Promise((_, reject) => {
          rejectRequest = reject
        })
    )
    renderWithQuery(
      <LookupPage search={DEFAULT_SEARCH} updateSearch={vi.fn()} />
    )

    expect(screen.getByText("正在載入金融商品與宏觀序列")).toBeInTheDocument()
    rejectRequest?.(new Error("查詢失敗"))
    expect(await screen.findByRole("alert")).toHaveTextContent("查詢失敗")
    expect(screen.queryByText("沒有符合條件的標的")).not.toBeInTheDocument()
  })

  it("uses a new query key when the validated search changes", async () => {
    const updateSearch = vi.fn()
    const { rerender } = renderWithQuery(
      <LookupPage search={DEFAULT_SEARCH} updateSearch={updateSearch} />
    )
    await screen.findByText("台積電")

    const nextSearch = { ...DEFAULT_SEARCH, q: "2330" }
    rerender(
      <QueryClientProvider
        client={
          new QueryClient({
            defaultOptions: { queries: { retry: false, gcTime: 0 } },
          })
        }
      >
        <LookupPage search={nextSearch} updateSearch={updateSearch} />
      </QueryClientProvider>
    )
    await waitFor(() =>
      expect(mocks.loadLookupPage).toHaveBeenCalledWith(
        nextSearch,
        expect.any(AbortSignal)
      )
    )
  })

  it("replaces an out-of-range page with the final available page", async () => {
    const updateSearch = vi.fn()
    const search = { ...DEFAULT_SEARCH, p: 9 }
    renderWithQuery(<LookupPage search={search} updateSearch={updateSearch} />)

    await waitFor(() =>
      expect(updateSearch).toHaveBeenCalledWith({
        ...search,
        p: 3,
        id: "",
      })
    )
  })
})
