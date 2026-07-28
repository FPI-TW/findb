import { describe, expect, it } from "vitest"

import {
  authenticateDashboardUser,
  configFromEnvironment,
  dashboardCookieOptions,
  fetchDashboardSession,
} from "./auth.server"

const user = {
  user_id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
  username: "operator",
  display_name: "Operations",
  role: "operator",
  is_active: true,
  must_change_password: false,
}

describe("dashboard backend authentication boundary", () => {
  it("does not require shared admin key or environment credentials", () => {
    expect(configFromEnvironment({})).toEqual({
      apiBaseUrl: "http://localhost:8080",
    })
    expect(
      configFromEnvironment({
        FINDB_API_BASE_URL: "https://findb.internal",
      })
    ).toEqual({ apiBaseUrl: "https://findb.internal" })
  })

  it("uses a private eight-hour cookie and keeps local HTTP development usable", () => {
    expect(dashboardCookieOptions("https", "production")).toEqual({
      httpOnly: true,
      secure: true,
      sameSite: "strict",
      path: "/dashboard",
      maxAge: 8 * 60 * 60,
    })
    expect(dashboardCookieOptions("http", "development").secure).toBe(false)
  })

  it("forwards login credentials to the backend and returns its opaque token", async () => {
    const calls: Array<{ url: string; init: RequestInit | undefined }> = []
    const result = await authenticateDashboardUser(
      "operator",
      "correct-password",
      async (input, init) => {
        calls.push({ url: input.toString(), init })
        return Response.json({
          access_token: "opaque-backend-session",
          token_type: "bearer",
          expires_at: "2026-07-28T10:00:00Z",
          user,
        })
      }
    )

    expect(result.access_token).toBe("opaque-backend-session")
    expect(calls[0]?.url).toBe("http://localhost:8080/api/v1/admin/auth/login")
    expect(calls[0]?.init?.method).toBe("POST")
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
      username: "operator",
      password: "correct-password",
    })
  })

  it("checks /auth/me with Bearer and never places the token in the URL", async () => {
    const calls: Array<{ url: string; authorization: string | null }> = []
    const result = await fetchDashboardSession(
      "opaque-backend-session",
      async (input, init) => {
        calls.push({
          url: input.toString(),
          authorization: new Headers(init?.headers).get("Authorization"),
        })
        return Response.json({ user })
      }
    )

    expect(result).toMatchObject({ username: "operator", role: "operator" })
    expect(calls).toEqual([
      {
        url: "http://localhost:8080/api/v1/admin/auth/me",
        authorization: "Bearer opaque-backend-session",
      },
    ])
    expect(calls[0]?.url).not.toContain("opaque-backend-session")
  })

  it("treats rejected backend sessions as signed out", async () => {
    const result = await fetchDashboardSession(
      "expired-session",
      async () => new Response(null, { status: 401 })
    )
    expect(result).toBeNull()
  })
})
