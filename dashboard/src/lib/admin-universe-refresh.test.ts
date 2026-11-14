import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const session = vi.hoisted(() => ({ require: vi.fn() }))
vi.mock("@tanstack/react-start", () => ({
  createServerFn: () => {
    const builder = {
      validator: () => builder,
      handler: (handler: unknown) => handler,
    }
    return builder
  },
}))
vi.mock("./auth.server", () => ({
  requireDashboardSession: session.require,
  assertSameOrigin: () => {},
  getDashboardConfig: () => ({ apiBaseUrl: "http://fixture.internal" }),
  markPrivateResponse: () => {},
  withDashboardAuthentication: (action: () => Promise<unknown>) => action(),
}))
import { requestUniverseRefresh } from "./admin.functions"
import {
  fetchUniverseFeedsData,
  requestUniverseRefreshData,
} from "./admin.server"

const timestamp = "2026-11-13T02:00:00Z"
const refresh = {
  work_id: "019565d2-f838-7c91-85c1-72d4d7bbbe92",
  provider: "finlab",
  dataset_key: "tw_etf_eod",
  state: "queued",
  requested_by: "Tyler",
  requested_at: "2026-11-13T02:00:00+00:00",
  note: "已核對交易所公告的新上市商品",
  completed_at: null,
  failure_code: null,
}
const request = {
  provider: "finlab",
  datasetKey: "tw_etf_eod",
  note: "已核對交易所公告的新上市商品",
}
type Invoke = (input: { data: typeof request }) => Promise<unknown>

afterEach(() => vi.unstubAllGlobals())

describe("universe refresh server boundary", () => {
  beforeEach(() => {
    session.require.mockResolvedValue({
      token: "fixture-session",
      user: { role: "owner" },
    })
  })

  it("reads the feeds with the session token", async () => {
    const calls: Array<{ url: URL; init: RequestInit | undefined }> = []
    const feeds = await fetchUniverseFeedsData(
      "viewer-secret",
      "https://findb.internal:8443",
      async (input, init) => {
        calls.push({ url: new URL(input.toString()), init })
        return Response.json({
          data: [
            {
              provider: "finlab",
              dataset_key: "tw_etf_eod",
              published: {
                release_id: "019565d2-f838-7c91-85c1-72d4d7bbbe90",
                dataset_key: "tw_etf_eod",
                provider: "finlab",
                effective_date: "2026-11-12",
                observed_at: timestamp,
                status: "published",
                member_count: 368,
                member_sha256: "a".repeat(64),
                semantic_member_sha256: "b".repeat(64),
                change_count: 7,
                change_ratio: 0.0192,
                created_at: timestamp,
                published_at: timestamp,
                published_by: "policy",
                approval_note: null,
                comparison_release_id: null,
              },
              candidate: null,
              refresh,
            },
          ],
        })
      }
    )

    expect(feeds.data[0]?.refresh?.state).toBe("queued")
    expect(calls[0]?.url.pathname).toBe("/api/v1/admin/universes/feeds")
    expect(new Headers(calls[0]?.init?.headers).get("Authorization")).toBe(
      "Bearer viewer-secret"
    )
  })

  it("posts the request and explains conflicts without leaking the upstream body", async () => {
    const bodies: unknown[] = []
    const accepted = await requestUniverseRefreshData(
      request,
      "owner-secret",
      undefined,
      async (_input, init) => {
        bodies.push(JSON.parse(String(init?.body)))
        return Response.json(refresh, { status: 202 })
      }
    )
    expect(accepted.work_id).toBe(refresh.work_id)
    expect(bodies).toEqual([
      {
        provider: "finlab",
        dataset_key: "tw_etf_eod",
        evidence_note: "已核對交易所公告的新上市商品",
      },
    ])

    const failing = (status: number) =>
      requestUniverseRefreshData(
        request,
        "owner-secret",
        undefined,
        async () => new Response("sensitive upstream body", { status })
      )
    await expect(failing(409)).rejects.toThrow("已有進行中的手動更新")
    await expect(failing(404)).rejects.toThrow("不支援手動更新名單")
    await expect(failing(401)).rejects.toThrow(
      "Dashboard authentication required"
    )
    await expect(failing(500)).rejects.toThrow("FinDB API request failed (500)")
  })

  it.each(["viewer", "operator"])(
    "refuses a %s before any upstream write",
    async role => {
      session.require.mockResolvedValue({
        token: "fixture-session",
        user: { role },
      })
      const transport = vi.fn()
      vi.stubGlobal("fetch", transport)

      await expect(
        (requestUniverseRefresh as unknown as Invoke)({ data: request })
      ).rejects.toThrow("沒有執行此操作的權限。")
      expect(transport).not.toHaveBeenCalled()
    }
  )

  it("lets an owner request through the session token", async () => {
    const transport = vi.fn(async () => Response.json(refresh, { status: 202 }))
    vi.stubGlobal("fetch", transport)

    await expect(
      (requestUniverseRefresh as unknown as Invoke)({ data: request })
    ).resolves.toMatchObject({ work_id: refresh.work_id })
    expect(transport).toHaveBeenCalledTimes(1)
  })
})
