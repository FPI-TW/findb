import { createServerFn } from "@tanstack/react-start"
import { z } from "zod"

import {
  assertSameOrigin,
  clearDashboardSession,
  credentialsMatch,
  enforceLoginRateLimit,
  getDashboardConfig,
  getDashboardSession,
  issueDashboardSession,
  markPrivateResponse,
  requireDashboardSession,
} from "./auth.server"

const loginSchema = z.object({
  username: z.string().trim().min(1).max(200),
  password: z.string().min(1).max(500),
})

export const getSession = createServerFn({ method: "GET" }).handler(() => {
  markPrivateResponse()
  const session = getDashboardSession()
  return session
    ? { authenticated: true as const, username: session.username }
    : { authenticated: false as const, username: null }
})

export const login = createServerFn({ method: "POST" })
  .validator(loginSchema)
  .handler(({ data }) => {
    assertSameOrigin()
    enforceLoginRateLimit()
    const config = getDashboardConfig()
    if (!credentialsMatch(data.username, data.password, config)) {
      throw new Error("帳號或密碼錯誤。")
    }
    issueDashboardSession(config)
    markPrivateResponse()
    return { authenticated: true as const, username: config.username }
  })

export const logout = createServerFn({ method: "POST" }).handler(() => {
  assertSameOrigin()
  requireDashboardSession()
  clearDashboardSession()
  markPrivateResponse()
  return { authenticated: false as const }
})
