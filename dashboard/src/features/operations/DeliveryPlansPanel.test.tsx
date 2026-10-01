import "@testing-library/jest-dom/vitest"

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { cleanup, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

const load = vi.hoisted(() => vi.fn())
vi.mock("@tanstack/react-start", async importOriginal => ({
  ...(await importOriginal<typeof import("@tanstack/react-start")>()),
  useServerFn: () => load,
}))

import { DeliveryPlansPanel } from "./DeliveryPlansPanel"

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <DeliveryPlansPanel />
    </QueryClientProvider>
  )
}

afterEach(() => {
  cleanup()
  load.mockReset()
})

describe("DeliveryPlansPanel", () => {
  it("shows initial loading before distinguishing no data, missing and blocked gaps", async () => {
    let resolve: (value: unknown) => void = () => undefined
    load.mockImplementation(
      () =>
        new Promise(value => {
          resolve = value
        })
    )
    renderPanel()
    expect(screen.getByRole("status")).toHaveTextContent(
      "正在載入全市場交付計畫"
    )
    resolve({
      data: [
        {
          plan_id: "p1",
          dataset_key: "tw_futures_eod",
          provider: "shioaji",
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
              { member_key: "MTX", status: "blocked", reason: "quota" },
            ],
          },
        },
      ],
    })
    expect(await screen.findByText("正常無資料")).toBeInTheDocument()
    expect(screen.getByText("TX")).toBeInTheDocument()
    expect(screen.getByText(/quota/)).toBeInTheDocument()
    expect(screen.getByText("已逾時")).toBeInTheDocument()
  })

  it("keeps API failure distinct from a successful empty list", async () => {
    load.mockRejectedValueOnce(new Error("HTTP 503"))
    const view = renderPanel()
    expect(await screen.findByText(/HTTP 503/)).toBeInTheDocument()
    view.unmount()
    load.mockResolvedValueOnce({ data: [] })
    renderPanel()
    expect(await screen.findByText("此條件沒有交付計畫。")).toBeInTheDocument()
  })
})
