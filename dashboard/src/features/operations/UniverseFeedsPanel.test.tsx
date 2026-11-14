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
import type { AdminRole } from "../../lib/admin-governance-api"

const mocks = vi.hoisted(() => ({
  loadUniverseFeeds: vi.fn(),
  requestUniverseRefresh: vi.fn(),
}))

vi.mock("@tanstack/react-start", async importOriginal => ({
  ...(await importOriginal<typeof import("@tanstack/react-start")>()),
  useServerFn: (serverFn: unknown) => serverFn,
}))
vi.mock("../../lib/admin.functions", () => mocks)

import { UniverseFeedsPanel } from "./UniverseFeedsPanel"

const release = {
  release_id: "019565d2-f838-7c91-85c1-72d4d7bbbe90",
  dataset_key: "tw_etf_eod",
  provider: "finlab",
  effective_date: "2026-11-12",
  observed_at: "2026-11-12T01:20:00Z",
  status: "published" as const,
  member_count: 368,
  member_sha256: "a".repeat(64),
  semantic_member_sha256: "b".repeat(64),
  change_count: 7,
  change_ratio: 0.0192,
  created_at: "2026-11-12T01:20:23Z",
  published_at: "2026-11-12T01:20:23Z",
  published_by: "policy",
  approval_note: null,
  comparison_release_id: null,
}
const candidate = {
  ...release,
  release_id: "019565d2-f838-7c91-85c1-72d4d7bbbe91",
  effective_date: "2026-11-13",
  status: "candidate" as const,
  change_count: 9,
  change_ratio: 0.0245,
  published_at: null,
  published_by: null,
}
const refresh = {
  work_id: "019565d2-f838-7c91-85c1-72d4d7bbbe92",
  provider: "finlab",
  dataset_key: "tw_etf_eod",
  state: "queued",
  requested_by: "Tyler",
  requested_at: "2026-11-13T02:00:00Z",
  note: "已核對交易所公告的新上市商品",
  completed_at: null,
  failure_code: null,
}
const feeds = (overrides: Record<string, unknown> = {}) => ({
  data: [
    {
      provider: "finlab",
      dataset_key: "tw_equity_eod",
      published: null,
      candidate: null,
      refresh: null,
    },
    {
      provider: "finlab",
      dataset_key: "tw_etf_eod",
      published: release,
      candidate,
      refresh: null,
      ...overrides,
    },
  ],
})

function renderPanel(role: AdminRole = "owner", active = true) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <UniverseFeedsPanel role={role} active={active} />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  mocks.loadUniverseFeeds.mockResolvedValue(feeds())
  mocks.requestUniverseRefresh.mockResolvedValue(refresh)
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe("universe feeds", () => {
  it("shows each feed's published list, waiting candidate and missing baseline", async () => {
    renderPanel()

    expect(await screen.findByText("tw_etf_eod")).toBeInTheDocument()
    expect(screen.getByText("生效日 2026-11-12 · 368 檔")).toBeInTheDocument()
    expect(screen.getByText("2026-11-13 · 變更 9（2.45%）")).toBeInTheDocument()
    expect(screen.getByText("尚未發布 baseline")).toBeInTheDocument()
  })

  it("asks for a reason and explains the approval before an owner refreshes", async () => {
    renderPanel()

    fireEvent.click(
      await screen.findByRole("button", { name: "手動更新 tw_etf_eod 名單" })
    )
    const dialog = await screen.findByRole("alertdialog")
    expect(dialog).toHaveTextContent("手動更新等同核准")
    expect(dialog).toHaveTextContent("不受 2% 變更門檻限制")
    expect(dialog).toHaveTextContent("名單內容相同時會直接發布它")
    const confirm = within(dialog).getByRole("button", {
      name: "確認更新並核准",
    })
    expect(confirm).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText("更新原因"), {
      target: { value: "  已核對交易所公告的新上市商品  " },
    })
    expect(confirm).not.toBeDisabled()
    mocks.loadUniverseFeeds.mockResolvedValue(feeds({ refresh }))
    fireEvent.click(confirm)

    await waitFor(() =>
      expect(mocks.requestUniverseRefresh).toHaveBeenCalledWith({
        data: {
          provider: "finlab",
          datasetKey: "tw_etf_eod",
          note: "已核對交易所公告的新上市商品",
        },
      })
    )
    // The list is read again and the pending request blocks a second click.
    expect(await screen.findByText("排隊中")).toBeInTheDocument()
    expect(
      screen.getByRole("button", { name: "手動更新 tw_etf_eod 名單" })
    ).toBeDisabled()
    expect(
      screen.getByRole("button", { name: "手動更新 tw_equity_eod 名單" })
    ).not.toBeDisabled()
  })

  it("shows the outcome of the last request and allows the next one", async () => {
    mocks.loadUniverseFeeds.mockResolvedValue(
      feeds({
        refresh: {
          ...refresh,
          state: "failed",
          completed_at: "2026-11-13T02:05:00Z",
          failure_code: "MAINTENANCE_FORMAT_OR_SCOPE",
        },
      })
    )
    renderPanel()

    expect(await screen.findByText("失敗")).toBeInTheDocument()
    expect(screen.getByText("MAINTENANCE_FORMAT_OR_SCOPE")).toBeInTheDocument()
    expect(
      screen.getByRole("button", { name: "手動更新 tw_etf_eod 名單" })
    ).not.toBeDisabled()
  })

  it.each(["viewer", "operator"] as const)(
    "is read-only for a %s",
    async role => {
      renderPanel(role)

      expect(await screen.findByText("tw_etf_eod")).toBeInTheDocument()
      expect(screen.queryByRole("button", { name: /手動更新/ })).toBeNull()
      expect(screen.getAllByText("僅 owner 可更新")).toHaveLength(2)
    }
  )

  it("does not query while its tab is hidden", () => {
    renderPanel("owner", false)

    expect(mocks.loadUniverseFeeds).not.toHaveBeenCalled()
  })
})
