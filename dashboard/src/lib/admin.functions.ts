import { createServerFn } from "@tanstack/react-start"
import { setResponseHeader } from "@tanstack/react-start/server"

import {
  dashboardRequestSchema,
  rawPayloadDetailRequestSchema,
  schedulerMutationRequestSchema,
  historicalBackfillCancelSchema,
  historicalBackfillCreateSchema,
  historicalBackfillPreviewSchema,
} from "./admin-api"
import {
  fetchDashboardData,
  fetchRawPayloadDetailData,
  patchSchedulerData,
  cancelHistoricalBackfillData,
  createHistoricalBackfillData,
  previewHistoricalBackfillData,
} from "./admin.server"
import {
  assertSameOrigin,
  getDashboardConfig,
  markPrivateResponse,
  requireDashboardSession,
} from "./auth.server"

export const loadDashboard = createServerFn({ method: "POST" })
  .validator(dashboardRequestSchema)
  .handler(async ({ data }) => {
    const config = getDashboardConfig()
    const session = await requireDashboardSession()
    setResponseHeader("Cache-Control", "no-store")
    setResponseHeader("Vary", "Cookie")
    return fetchDashboardData(
      data,
      session.token,
      config.apiBaseUrl,
      fetch,
      session.user.role
    )
  })

export const loadRawPayloadDetail = createServerFn({ method: "POST" })
  .validator(rawPayloadDetailRequestSchema)
  .handler(async ({ data }) => {
    const config = getDashboardConfig()
    const session = await requireDashboardSession()
    setResponseHeader("Cache-Control", "no-store")
    setResponseHeader("Vary", "Cookie")
    return fetchRawPayloadDetailData(data, session.token, config.apiBaseUrl)
  })

export const updateScheduler = createServerFn({ method: "POST" })
  .validator(schedulerMutationRequestSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const session = await requireDashboardSession()
    if (session.user.role !== "owner") {
      throw new Error("沒有執行此操作的權限。")
    }
    const config = getDashboardConfig()
    markPrivateResponse()
    return patchSchedulerData(data, session.token, config.apiBaseUrl)
  })

export const createHistoricalBackfill = createServerFn({ method: "POST" })
  .validator(historicalBackfillCreateSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const session = await requireDashboardSession()
    if (session.user.role === "viewer")
      throw new Error("沒有執行此操作的權限。")
    const config = getDashboardConfig()
    markPrivateResponse()
    return createHistoricalBackfillData(data, session.token, config.apiBaseUrl)
  })

export const cancelHistoricalBackfill = createServerFn({ method: "POST" })
  .validator(historicalBackfillCancelSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const session = await requireDashboardSession()
    if (session.user.role === "viewer")
      throw new Error("沒有執行此操作的權限。")
    const config = getDashboardConfig()
    markPrivateResponse()
    return cancelHistoricalBackfillData(
      data.requestId,
      session.token,
      config.apiBaseUrl
    )
  })

export const previewHistoricalBackfill = createServerFn({ method: "POST" })
  .validator(historicalBackfillPreviewSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const session = await requireDashboardSession()
    if (session.user.role === "viewer")
      throw new Error("沒有執行此操作的權限。")
    const config = getDashboardConfig()
    markPrivateResponse()
    return previewHistoricalBackfillData(data, session.token, config.apiBaseUrl)
  })
