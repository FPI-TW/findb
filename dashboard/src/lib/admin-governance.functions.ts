import { createServerFn } from "@tanstack/react-start"

import {
  credentialFiltersSchema,
  credentialMutationResponseSchema,
  credentialRevokeResponseSchema,
  credentialsOverviewSchema,
  credentialsResponseSchema,
  credentialTargetSchema,
  createCredentialSchema,
  createUserSchema,
  resetUserPasswordSchema,
  updateUserSchema,
  userMutationResponseSchema,
  usersResponseSchema,
  type AdminRole,
} from "./admin-governance-api"
import {
  assertSameOrigin,
  getDashboardConfig,
  markPrivateResponse,
  requireDashboardSession,
} from "./auth.server"

type JsonRequest = {
  method?: "GET" | "POST" | "PATCH" | "DELETE"
  body?: unknown
}

async function adminRequest(
  path: string,
  request: JsonRequest = {},
  allowedRoles: AdminRole[] = ["owner", "operator", "viewer"]
) {
  const session = await requireDashboardSession()
  if (!allowedRoles.includes(session.user.role)) {
    throw new Error("沒有執行此操作的權限。")
  }
  const baseUrl = new URL(getDashboardConfig().apiBaseUrl)
  if (!["http:", "https:"].includes(baseUrl.protocol)) {
    throw new Error("FinDB API base URL must use HTTP or HTTPS")
  }
  let response: Response
  try {
    const init: RequestInit = {
      method: request.method ?? "GET",
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${session.token}`,
        ...(request.body === undefined
          ? {}
          : { "Content-Type": "application/json" }),
      },
      cache: "no-store",
    }
    if (request.body !== undefined) {
      init.body = JSON.stringify(request.body)
    }
    response = await fetch(new URL(path, baseUrl), init)
  } catch {
    throw new Error("無法連線至 FinDB API。")
  }
  if (!response.ok) {
    if (response.status === 401) throw new Error("登入已失效，請重新登入。")
    if (response.status === 403) throw new Error("沒有執行此操作的權限。")
    try {
      const payload = (await response.json()) as {
        detail?: string
        message?: string
      }
      throw new Error(
        payload.detail ||
          payload.message ||
          `FinDB API request failed (${response.status})`
      )
    } catch (error) {
      if (error instanceof Error && !error.message.startsWith("Unexpected")) {
        throw error
      }
      throw new Error(`FinDB API request failed (${response.status})`)
    }
  }
  markPrivateResponse()
  return response.json()
}

export const loadCredentials = createServerFn({ method: "GET" })
  .validator(credentialFiltersSchema)
  .handler(async ({ data }) => {
    const search = new URLSearchParams()
    if (data.kind) search.set("kind", data.kind)
    if (data.status) search.set("status", data.status)
    if (data.owner) search.set("owner", data.owner)
    const suffix = search.size ? `?${search.toString()}` : ""
    const payload = await adminRequest(`/api/v1/admin/credentials${suffix}`)
    return credentialsResponseSchema.parse(payload)
  })

export const loadCredentialsOverview = createServerFn({
  method: "GET",
}).handler(async () => {
  const payload = await adminRequest("/api/v1/admin/credentials/overview")
  return credentialsOverviewSchema.parse(payload)
})

export const issueCredential = createServerFn({ method: "POST" })
  .validator(createCredentialSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const roles: AdminRole[] =
      data.kind === "admin" ? ["owner"] : ["owner", "operator"]
    const payload = await adminRequest(
      "/api/v1/admin/credentials",
      { method: "POST", body: data },
      roles
    )
    return credentialMutationResponseSchema.parse(payload)
  })

export const rotateCredential = createServerFn({ method: "POST" })
  .validator(credentialTargetSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const roles: AdminRole[] =
      data.kind === "admin" ? ["owner"] : ["owner", "operator"]
    const payload = await adminRequest(
      `/api/v1/admin/credentials/${data.kind}/${data.id}/rotate`,
      { method: "POST" },
      roles
    )
    return credentialMutationResponseSchema.parse(payload)
  })

export const revokeCredential = createServerFn({ method: "POST" })
  .validator(credentialTargetSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const roles: AdminRole[] =
      data.kind === "admin" ? ["owner"] : ["owner", "operator"]
    const payload = await adminRequest(
      `/api/v1/admin/credentials/${data.kind}/${data.id}`,
      { method: "DELETE" },
      roles
    )
    return credentialRevokeResponseSchema.parse(payload)
  })

export const loadUsers = createServerFn({ method: "GET" }).handler(async () => {
  const payload = await adminRequest("/api/v1/admin/users", {}, ["owner"])
  return usersResponseSchema.parse(payload)
})

export const createUser = createServerFn({ method: "POST" })
  .validator(createUserSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const payload = await adminRequest(
      "/api/v1/admin/users",
      { method: "POST", body: data },
      ["owner"]
    )
    return userMutationResponseSchema.parse(payload)
  })

export const updateUser = createServerFn({ method: "POST" })
  .validator(updateUserSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const { userId, ...body } = data
    const payload = await adminRequest(
      `/api/v1/admin/users/${userId}`,
      { method: "PATCH", body },
      ["owner"]
    )
    return userMutationResponseSchema.parse(payload)
  })

export const resetUserPassword = createServerFn({ method: "POST" })
  .validator(resetUserPasswordSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const payload = await adminRequest(
      `/api/v1/admin/users/${data.userId}/reset-password`,
      {
        method: "POST",
        body: data.password ? { password: data.password } : {},
      },
      ["owner"]
    )
    return userMutationResponseSchema.parse(payload)
  })
