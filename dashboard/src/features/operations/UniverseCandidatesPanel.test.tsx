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

const mocks = vi.hoisted(() => ({
  loadUniverseCandidates: vi.fn(),
  loadUniverseDiff: vi.fn(),
}))

vi.mock("@tanstack/react-start", async importOriginal => ({
  ...(await importOriginal<typeof import("@tanstack/react-start")>()),
  useServerFn: (serverFn: unknown) => serverFn,
}))
vi.mock("../../lib/admin.functions", () => mocks)

import { UniverseCandidatesPanel } from "./UniverseCandidatesPanel"

const releaseId = "019565d2-f838-7c91-85c1-72d4d7bbbe90"
const comparisonId = "019565d2-f838-7c91-85c1-72d4d7bbbe91"
const release = {
  release_id: releaseId,
  dataset_key: "tw_futures_eod",
  provider: "taifex",
  effective_date: "2026-10-15",
  observed_at: "2026-10-15T12:00:00Z",
  status: "candidate" as const,
  member_count: 2,
  member_sha256: "a".repeat(64),
  semantic_member_sha256: "b".repeat(64),
  change_count: 2,
  change_ratio: 0.5,
  created_at: "2026-10-15T12:01:00Z",
  published_at: null,
  published_by: null,
  approval_note: null,
  comparison_release_id: comparisonId,
}
const member = (memberKey: string) => ({
  member_key: memberKey,
  symbol: memberKey,
  provider_symbol: memberKey,
  exchange: "TAIFEX",
  currency: "TWD",
  asset_class: "future",
  classification: null,
  contract_code: memberKey,
  product_code: "TX",
  contract_month: "202610",
  session: "regular",
  mapping_status: "mapped",
})

function renderPanel(active = true) {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <UniverseCandidatesPanel active={active} />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  mocks.loadUniverseCandidates.mockResolvedValue({
    data: [release],
    pagination: {
      page: 1,
      page_size: 25,
      total_records: 1,
      total_pages: 1,
    },
  })
  mocks.loadUniverseDiff.mockResolvedValue({
    release,
    comparison_release: {
      ...release,
      release_id: comparisonId,
      status: "published",
    },
    added: [member("TX202610")],
    removed: [member("TX202609")],
    changed: [
      {
        member_key: "MTX202610",
        changed_fields: ["session"],
        before: { ...member("MTX202610"), session: "after_hours" },
        after: member("MTX202610"),
      },
    ],
    unchanged_count: 18,
  })
})

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

describe("universe candidates", () => {
  it("lists candidates and loads the selected member diff", async () => {
    renderPanel()

    expect(await screen.findByText("tw_futures_eod")).toBeInTheDocument()
    expect(mocks.loadUniverseCandidates).toHaveBeenCalledWith({
      data: {
        datasetKey: "",
        provider: "",
        effectiveDate: "",
        page: 1,
        pageSize: 25,
      },
    })

    fireEvent.click(
      screen.getByRole("button", { name: `檢視 ${releaseId} 成員差異` })
    )

    await waitFor(() =>
      expect(mocks.loadUniverseDiff).toHaveBeenCalledWith({
        data: { releaseId },
      })
    )
    expect(await screen.findByText("TX202610")).toBeInTheDocument()
    expect(screen.getByText("TX202609")).toBeInTheDocument()
    expect(screen.getAllByText("MTX202610")).toHaveLength(2)
    expect(screen.getByText("變更欄位：session")).toBeInTheDocument()
    expect(screen.getByText("未變更成員：18")).toBeInTheDocument()
  })

  it("does not query while its tab is hidden", () => {
    renderPanel(false)

    expect(mocks.loadUniverseCandidates).not.toHaveBeenCalled()
    expect(mocks.loadUniverseDiff).not.toHaveBeenCalled()
  })
})
