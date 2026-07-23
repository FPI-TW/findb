import { describe, expect, it } from "vitest"

import {
  configFromEnvironment,
  createSessionToken,
  credentialsMatch,
  verifySessionToken,
} from "./auth.server"

const environment = {
  ADMIN_API_KEY: "admin-secret",
  FINDB_API_BASE_URL: "https://findb.internal",
  DASHBOARD_USERNAME: "operator",
  DASHBOARD_PASSWORD: "correct-horse-battery-staple",
  DASHBOARD_SESSION_SECRET:
    "a-secure-session-secret-with-more-than-32-characters",
}

describe("dashboard authentication primitives", () => {
  it("requires all server-side secrets", () => {
    expect(() => configFromEnvironment({})).toThrow(
      "ADMIN_API_KEY is not configured"
    )
    expect(() =>
      configFromEnvironment({
        ...environment,
        DASHBOARD_SESSION_SECRET: "too-short",
      })
    ).toThrow("DASHBOARD_SESSION_SECRET is not configured")
  })

  it("compares configured credentials without returning secrets", () => {
    const config = configFromEnvironment(environment)
    expect(
      credentialsMatch("operator", "correct-horse-battery-staple", config)
    ).toBe(true)
    expect(credentialsMatch("operator", "wrong-password", config)).toBe(false)
    expect(credentialsMatch("wrong-user", config.password, config)).toBe(false)
  })

  it("signs expiring sessions and rejects tampering", () => {
    const config = configFromEnvironment(environment)
    const now = Date.UTC(2026, 6, 23)
    const token = createSessionToken(config, now)

    expect(verifySessionToken(token, config, now + 1_000)).toMatchObject({
      username: "operator",
    })
    expect(
      verifySessionToken(`${token.slice(0, -1)}x`, config, now + 1_000)
    ).toBeNull()
    expect(
      verifySessionToken(
        `${token.slice(0, token.lastIndexOf(".") + 1)}!`,
        config
      )
    ).toBeNull()
    expect(
      verifySessionToken(token, config, now + 9 * 60 * 60 * 1000)
    ).toBeNull()
  })

  it("invalidates existing sessions when credentials change", () => {
    const config = configFromEnvironment(environment)
    const token = createSessionToken(config)
    const rotated = { ...config, password: "new-strong-password" }

    expect(verifySessionToken(token, rotated)).toBeNull()
  })
})
