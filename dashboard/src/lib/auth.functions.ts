import { createServerFn } from "@tanstack/react-start"
import { z } from "zod"

import {
  assertSameOrigin,
  authenticateDashboardUser,
  changeDashboardPassword,
  clearDashboardSession,
  fetchDashboardSession,
  issueDashboardSession,
  markPrivateResponse,
  revokeDashboardSession,
} from "./auth.server"

const loginSchema = z.object({
  username: z.string().trim().min(1).max(200),
  password: z.string().min(1).max(500),
})

const changePasswordSchema = z
  .object({
    currentPassword: z.string().min(1).max(500),
    newPassword: z.string().min(12).max(500),
    confirmPassword: z.string().min(1).max(500),
  })
  .refine(value => value.newPassword === value.confirmPassword, {
    message: "兩次輸入的新密碼不一致。",
    path: ["confirmPassword"],
  })

export const getSession = createServerFn({ method: "GET" }).handler(
  async () => {
    markPrivateResponse()
    const user = await fetchDashboardSession()
    if (!user) {
      clearDashboardSession()
      return {
        authenticated: false as const,
        username: null,
        role: null,
        mustChangePassword: false,
      }
    }
    return {
      authenticated: true as const,
      username: user.username,
      role: user.role,
      mustChangePassword: user.must_change_password,
    }
  }
)

export const login = createServerFn({ method: "POST" })
  .validator(loginSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const result = await authenticateDashboardUser(data.username, data.password)
    issueDashboardSession(result.access_token)
    markPrivateResponse()
    return {
      authenticated: true as const,
      username: result.user.username,
      role: result.user.role,
      mustChangePassword: result.user.must_change_password,
    }
  })

export const logout = createServerFn({ method: "POST" }).handler(async () => {
  assertSameOrigin()
  await revokeDashboardSession()
  clearDashboardSession()
  markPrivateResponse()
  return { authenticated: false as const }
})

export const changePassword = createServerFn({ method: "POST" })
  .validator(changePasswordSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    await changeDashboardPassword(data.currentPassword, data.newPassword)
    clearDashboardSession()
    markPrivateResponse()
    return { success: true as const }
  })
