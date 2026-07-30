import type { AdminRole } from "./admin-governance-api"
import {
  getDashboardConfig,
  markPrivateResponse,
  requireDashboardSession,
} from "./auth.server"

type Method = "GET" | "POST" | "PATCH"

async function request(
  path: string,
  init: RequestInit,
  allowedRoles: AdminRole[] = ["owner", "operator", "viewer"]
) {
  const session = await requireDashboardSession()
  if (!allowedRoles.includes(session.user.role))
    throw new Error("沒有執行此操作的權限。")
  const baseUrl = new URL(getDashboardConfig().apiBaseUrl)
  if (!["http:", "https:"].includes(baseUrl.protocol))
    throw new Error("FinDB API base URL must use HTTP or HTTPS")
  let response: Response
  try {
    response = await fetch(new URL(path, baseUrl), {
      ...init,
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${session.token}`,
        ...init.headers,
      },
      cache: "no-store",
    })
  } catch {
    throw new Error("無法連線至 FinDB API。")
  }
  if (!response.ok) {
    if (response.status === 401) throw new Error("登入已失效，請重新登入。")
    if (response.status === 403) throw new Error("沒有執行此操作的權限。")
    if (response.status === 409)
      throw new Error("交易日曆已被其他管理者更新，請重新載入後再確認。")
    const payload = (await response.json().catch(() => null)) as {
      detail?: string
      message?: string
    } | null
    throw new Error(
      payload?.detail ||
        payload?.message ||
        `FinDB API request failed (${response.status})`
    )
  }
  markPrivateResponse()
  return response.json()
}

export function calendarRequest(path: string) {
  return request(path, { method: "GET" })
}
export function calendarJsonRequest(
  path: string,
  body: unknown,
  roles: AdminRole[],
  method: Method = "POST"
) {
  return request(
    path,
    {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    roles
  )
}
export function calendarMultipartRequest(
  path: string,
  body: FormData,
  roles: AdminRole[]
) {
  return request(path, { method: "POST", body }, roles)
}
