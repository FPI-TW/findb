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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const mocks = vi.hoisted(() => ({ loadEod: vi.fn(), loadMinute: vi.fn() }))
vi.mock("./data", () => mocks)

import { DetailDrawer } from "./DetailDrawer"
import type { Instrument } from "./types"

const instrument: Instrument = {
  instrument_id: "instrument-1",
  market: "TW",
  asset_class: "equity",
  symbol: "2330",
  name: "台積電",
  currency: "TWD",
  timezone: "Asia/Taipei",
  status: "active",
  listed_date: "1994-09-05",
  delisted_date: null,
  coverage: {
    eod: {
      first_date: "1994-09-05",
      latest_date: "2026-07-22",
      latest_close: "1085.5",
    },
    minute: {
      first_bar_at: "2026-07-22T01:00:00Z",
      latest_bar_at: "2026-07-22T05:30:00Z",
      latest_close: "1086",
    },
  },
}

function renderDrawer(item = instrument, onClose = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  const view = render(
    <QueryClientProvider client={queryClient}>
      <DetailDrawer item={item} onClose={onClose} onCopied={vi.fn()} />
    </QueryClientProvider>
  )
  return { ...view, onClose }
}

beforeEach(() => {
  mocks.loadEod
    .mockReset()
    .mockResolvedValue([
      { trade_date: "2026-07-22", close: "1085.5", volume: 1234 },
    ])
  mocks.loadMinute.mockReset().mockResolvedValue([
    {
      trade_date: "2026-07-22",
      bar_start_time: "2026-07-22T05:30:00Z",
      close: "1086",
      volume: 12,
    },
  ])
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("DetailDrawer", () => {
  it("loads EOD and minute feeds only when coverage exists", async () => {
    renderDrawer()
    expect(await screen.findByText("1085.5")).toBeInTheDocument()
    expect(await screen.findByText("2026-07-22T05:30:00Z")).toBeInTheDocument()
    expect(mocks.loadEod).toHaveBeenCalledWith(
      "instrument-1",
      expect.any(AbortSignal)
    )
    expect(mocks.loadMinute).toHaveBeenCalledWith(
      "instrument-1",
      expect.any(AbortSignal)
    )
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
  })

  it("shows unavailable and error states independently", async () => {
    mocks.loadEod.mockRejectedValue(new Error("EOD 服務失敗"))
    renderDrawer({
      ...instrument,
      coverage: { ...instrument.coverage, minute: null },
    })
    expect(await screen.findByText("EOD 服務失敗")).toBeInTheDocument()
    expect(
      screen.getByText("此商品沒有 active minute dataset coverage。")
    ).toBeInTheDocument()
    expect(mocks.loadMinute).not.toHaveBeenCalled()
  })

  it("contains keyboard focus and restores the prior focus", async () => {
    const trigger = document.createElement("button")
    document.body.append(trigger)
    trigger.focus()
    const { onClose, unmount } = renderDrawer()
    const dialog = screen.getByRole("dialog")
    const drawer = within(dialog)
    const close = drawer.getByRole("button", { name: "關閉詳情" })
    const copyId = drawer.getByRole("button", { name: "複製 ID" })
    await waitFor(() => expect(dialog).toHaveFocus())

    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true })
    expect(copyId).toHaveFocus()
    fireEvent.keyDown(copyId, { key: "Tab" })
    expect(close).toHaveFocus()
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true })
    expect(copyId).toHaveFocus()

    const hidden = document.createElement("button")
    hidden.hidden = true
    dialog.append(hidden)
    close.focus()
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true })
    expect(copyId).toHaveFocus()

    const buttons = drawer.getAllByRole("button")
    for (const button of buttons) button.setAttribute("disabled", "")
    dialog.focus()
    fireEvent.keyDown(dialog, { key: "Tab" })
    expect(dialog).toHaveFocus()
    close.removeAttribute("disabled")
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true })
    expect(close).toHaveFocus()
    fireEvent.keyDown(close, { key: "Tab" })
    expect(close).toHaveFocus()

    fireEvent.keyDown(close, { key: "Escape" })
    expect(onClose).toHaveBeenCalledOnce()
    unmount()
    expect(trigger).toHaveFocus()
    trigger.remove()
  })
})
