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
import { createUser } from "./admin-governance.functions"
import { calendarJsonRequest } from "./calendar.server"
import { DashboardAuthenticationError } from "./auth-errors"

beforeEach(() => {
  session.require.mockResolvedValue({
    token: "fixture-session",
    user: { role: "owner" },
  })
})
afterEach(() => vi.unstubAllGlobals())

describe("governance and calendar protected mutation transport", () => {
  const actions = [
    () =>
      createUser({
        data: {
          username: "fixture",
          display_name: "Fixture",
          role: "viewer",
          must_change_password: true,
        },
      }),
    () =>
      calendarJsonRequest(
        "/api/v1/admin/calendars/TW/2026/publish",
        { expected_revision: 1 },
        ["owner"]
      ),
  ]
  it.each(actions)(
    "signals local rejected sessions before attempting upstream writes",
    async action => {
      session.require.mockRejectedValue(new DashboardAuthenticationError())
      const transport = vi.fn()
      vi.stubGlobal("fetch", transport)
      await expect(action()).rejects.toThrow(
        "Dashboard authentication required"
      )
      expect(transport).not.toHaveBeenCalled()
    }
  )
  it.each(actions)(
    "normalizes upstream 401 while preserving permission and domain errors",
    async action => {
      const transport = vi
        .fn()
        .mockResolvedValue(
          Response.json({ detail: "Expired" }, { status: 401 })
        )
      vi.stubGlobal("fetch", transport)
      await expect(action()).rejects.toThrow(
        "Dashboard authentication required"
      )
      expect(transport.mock.calls[0]?.[1]).toMatchObject({
        cache: "no-store",
        headers: expect.objectContaining({
          Authorization: "Bearer fixture-session",
        }),
      })
      transport.mockResolvedValue(
        Response.json({ detail: "Insufficient admin role" }, { status: 403 })
      )
      await expect(action()).rejects.toThrow("沒有執行此操作的權限。")
      transport.mockResolvedValue(
        Response.json({ detail: "Domain conflict" }, { status: 409 })
      )
      await expect(action()).rejects.not.toThrow(
        "Dashboard authentication required"
      )
    }
  )
})
