import { basename, resolve } from "node:path"

import { config as loadDotenv } from "dotenv"
import {
  deleteCookie,
  getCookie,
  getRequest,
  setCookie,
  setResponseHeader,
} from "@tanstack/react-start/server"

import {
  adminLoginResponseSchema,
  adminSessionUserSchema,
  type AdminSessionUser,
} from "./admin-governance-api"
import { DashboardAuthenticationError } from "./auth-errors"

const SESSION_COOKIE = "findb_dashboard_session"
const SESSION_TTL_SECONDS = 8 * 60 * 60

export type DashboardConfig = {
  apiBaseUrl: string
}

export function configFromEnvironment(
  environment: NodeJS.ProcessEnv
): DashboardConfig {
  return {
    apiBaseUrl:
      environment.FINDB_API_BASE_URL?.trim() || "http://localhost:8080",
  }
}

export function getDashboardConfig() {
  const cwd = process.cwd()
  const repositoryRoot =
    basename(cwd) === "dashboard" ? resolve(cwd, "..") : cwd
  loadDotenv({
    path: resolve(repositoryRoot, ".env"),
    override: false,
    quiet: true,
  })
  return configFromEnvironment(process.env)
}

function apiUrl(path: string, config = getDashboardConfig()) {
  const baseUrl = new URL(config.apiBaseUrl)
  if (!["http:", "https:"].includes(baseUrl.protocol)) {
    throw new Error("FinDB API base URL must use HTTP or HTTPS")
  }
  return new URL(path, baseUrl)
}

export function dashboardCookieOptions(
  protocol: string,
  nodeEnvironment = process.env.NODE_ENV
) {
  return {
    httpOnly: true,
    secure: nodeEnvironment === "production" || protocol === "https",
    sameSite: "strict" as const,
    path: "/dashboard",
    maxAge: SESSION_TTL_SECONDS,
  }
}

function cookieOptions() {
  const request = getRequest()
  const forwardedProto = request.headers.get("x-forwarded-proto")
  const requestProtocol = new URL(request.url).protocol.replace(":", "")
  return dashboardCookieOptions(forwardedProto || requestProtocol)
}

export function issueDashboardSession(token: string) {
  setCookie(SESSION_COOKIE, token, cookieOptions())
}

export function clearDashboardSession() {
  deleteCookie(SESSION_COOKIE, cookieOptions())
}

export function getDashboardSessionToken() {
  return getCookie(SESSION_COOKIE) ?? null
}

async function parseError(response: Response, fallback: string) {
  try {
    const payload = (await response.json()) as {
      detail?: string
      message?: string
    }
    return payload.detail || payload.message || fallback
  } catch {
    return fallback
  }
}

export async function authenticateDashboardUser(
  username: string,
  password: string,
  fetchImplementation: typeof fetch = fetch
) {
  let response: Response
  try {
    response = await fetchImplementation(apiUrl("/api/v1/admin/auth/login"), {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ username, password }),
      cache: "no-store",
    })
  } catch {
    throw new Error("無法連線至 FinDB API。")
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new Error("帳號或密碼錯誤。")
    }
    if (response.status === 429) {
      throw new Error("登入嘗試過於頻繁，請稍後再試。")
    }
    throw new Error(await parseError(response, "登入失敗。"))
  }
  try {
    return adminLoginResponseSchema.parse(await response.json())
  } catch {
    throw new Error("FinDB API 回傳了無法辨識的登入結果。")
  }
}

export async function fetchDashboardSession(
  token = getDashboardSessionToken(),
  fetchImplementation: typeof fetch = fetch
): Promise<AdminSessionUser | null> {
  if (!token) return null
  let response: Response
  try {
    response = await fetchImplementation(apiUrl("/api/v1/admin/auth/me"), {
      method: "GET",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${token}`,
      },
      cache: "no-store",
    })
  } catch {
    throw new Error("無法確認登入狀態。")
  }
  if (response.status === 401 || response.status === 403) return null
  if (!response.ok) {
    throw new Error(await parseError(response, "無法確認登入狀態。"))
  }
  try {
    return adminSessionUserSchema.parse(await response.json())
  } catch {
    throw new Error("FinDB API 回傳了無法辨識的登入狀態。")
  }
}

export async function revokeDashboardSession(
  token = getDashboardSessionToken(),
  fetchImplementation: typeof fetch = fetch
) {
  if (!token) return
  try {
    await fetchImplementation(apiUrl("/api/v1/admin/auth/logout"), {
      method: "POST",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${token}`,
      },
      cache: "no-store",
    })
  } catch {
    // Clearing the browser cookie is still required when the backend is down.
  }
}

export async function changeDashboardPassword(
  currentPassword: string,
  newPassword: string,
  fetchImplementation: typeof fetch = fetch
) {
  const token = getDashboardSessionToken()
  if (!token) throw new Error("Unauthorized")
  let response: Response
  try {
    response = await fetchImplementation(
      apiUrl("/api/v1/admin/auth/change-password"),
      {
        method: "POST",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
        cache: "no-store",
      }
    )
  } catch {
    throw new Error("無法連線至 FinDB API。")
  }
  if (!response.ok) {
    throw new Error(await parseError(response, "密碼更新失敗。"))
  }
}

export async function requireDashboardSession() {
  const token = getDashboardSessionToken()
  if (!token) throw new DashboardAuthenticationError()
  const user = await fetchDashboardSession(token)
  if (!user) {
    clearDashboardSession()
    throw new DashboardAuthenticationError()
  }
  return { token, user }
}

export function assertSameOrigin() {
  const request = getRequest()
  const origin = request.headers.get("origin")
  if (!origin) return
  const forwardedProto = request.headers.get("x-forwarded-proto")
  const forwardedHost = request.headers.get("x-forwarded-host")
  const expectedOrigin = `${forwardedProto || new URL(request.url).protocol.replace(":", "")}://${forwardedHost || request.headers.get("host")}`
  if (new URL(origin).origin !== expectedOrigin) {
    throw new Error("Origin check failed")
  }
}

export function markPrivateResponse() {
  setResponseHeader("Cache-Control", "no-store")
  setResponseHeader("Vary", "Cookie")
}
