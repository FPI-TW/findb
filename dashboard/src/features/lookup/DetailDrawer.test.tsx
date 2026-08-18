import "@testing-library/jest-dom/vitest"

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const mocks = vi.hoisted(() => ({
  loadCorporateActions: vi.fn(),
  loadMacroObservations: vi.fn(),
  loadPrices: vi.fn(),
}))

vi.mock("./data", () => mocks)

import { DetailDrawer } from "./DetailDrawer"

const instrument = {
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
}

function renderDrawer() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <DetailDrawer
        dataset="instruments"
        item={instrument}
        onClose={vi.fn()}
        onCopied={vi.fn()}
      />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  mocks.loadPrices
    .mockReset()
    .mockResolvedValue([
      { trade_date: "2026-07-22", close: "1085.5", volume: 1234 },
    ])
  mocks.loadCorporateActions.mockReset().mockResolvedValue([
    {
      ex_date: "2026-07-01",
      action_type: "cash_dividend",
      cash_amount: 4.5,
      currency: "TWD",
    },
  ])
  mocks.loadMacroObservations.mockReset().mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("DetailDrawer", () => {
  it("loads ID-scoped feeds through DataTable and keeps close/copy controls", async () => {
    renderDrawer()

    expect(await screen.findByText("1085.5")).toBeInTheDocument()
    expect(screen.getByText("cash_dividend")).toBeInTheDocument()
    expect(
      screen.getByRole("columnheader", { name: "交易日" })
    ).toBeInTheDocument()
    expect(mocks.loadPrices).toHaveBeenCalledWith(
      "instrument-1",
      expect.any(AbortSignal)
    )
    expect(mocks.loadCorporateActions).toHaveBeenCalledWith(
      "instrument-1",
      expect.any(AbortSignal)
    )
    expect(
      screen.getAllByRole("button", { name: "關閉詳情" })
    ).not.toHaveLength(0)

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
  })

  it("renders independent loading, error, and empty feed states", async () => {
    mocks.loadPrices.mockRejectedValue(new Error("價格服務失敗"))
    mocks.loadCorporateActions.mockResolvedValue([])
    renderDrawer()

    expect(await screen.findByText("價格服務失敗")).toBeInTheDocument()
    expect(screen.getByText("目前沒有公司事件。")).toBeInTheDocument()
    expect(
      screen.getAllByRole("button", { name: "重試" }).length
    ).toBeGreaterThan(0)
  })
})
