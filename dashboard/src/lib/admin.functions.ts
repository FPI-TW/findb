import { createServerFn } from "@tanstack/react-start"
import { setResponseHeader } from "@tanstack/react-start/server"

import {
  dashboardRequestSchema,
  rawPayloadDetailRequestSchema,
  schedulerMutationRequestSchema,
  historicalBackfillCancelSchema,
  historicalBackfillCreateSchema,
  historicalBackfillPreviewSchema,
  deliveryPlansRequestSchema,
  deliveryResourceRequestSchema,
} from "./admin-api"
import {
  fetchDashboardData,
  fetchRawPayloadDetailData,
  patchSchedulerData,
  cancelHistoricalBackfillData,
  createHistoricalBackfillData,
  previewHistoricalBackfillData,
  fetchDeliveryPlansData,
  fetchDeliveryPlanDatasetsData,
  fetchDeliveryResourceData,
} from "./admin.server"
import {
  assertSameOrigin,
  getDashboardConfig,
  markPrivateResponse,
  requireDashboardSession,
  withDashboardAuthentication,
} from "./auth.server"

export const loadDashboard = createServerFn({ method: "POST" })
  .validator(dashboardRequestSchema)
  .handler(async ({ data }) => {
    const config = getDashboardConfig()
    const session = await requireDashboardSession()
    setResponseHeader("Cache-Control", "no-store")
    setResponseHeader("Vary", "Cookie")
    return withDashboardAuthentication(() =>
      fetchDashboardData(
        data,
        session.token,
        config.apiBaseUrl,
        fetch,
        session.user.role
      )
    )
  })

export const loadDeliveryPlans = createServerFn({ method: "POST" })
  .validator(deliveryPlansRequestSchema)
  .handler(async ({ data }) => {
    const config = getDashboardConfig()
    const session = await requireDashboardSession()
    setResponseHeader("Cache-Control", "no-store")
    setResponseHeader("Vary", "Cookie")
    return withDashboardAuthentication(() =>
      fetchDeliveryPlansData(data, session.token, config.apiBaseUrl)
    )
  })

export const loadRawPayloadDetail = createServerFn({ method: "POST" })
  .validator(rawPayloadDetailRequestSchema)
  .handler(async ({ data }) => {
    const config = getDashboardConfig()
    const session = await requireDashboardSession()
    setResponseHeader("Cache-Control", "no-store")
    setResponseHeader("Vary", "Cookie")
    return withDashboardAuthentication(() =>
      fetchRawPayloadDetailData(data, session.token, config.apiBaseUrl)
    )
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
    return withDashboardAuthentication(() =>
      patchSchedulerData(data, session.token, config.apiBaseUrl)
    )
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
    return withDashboardAuthentication(() =>
      createHistoricalBackfillData(data, session.token, config.apiBaseUrl)
    )
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
    return withDashboardAuthentication(() =>
      cancelHistoricalBackfillData(
        data.requestId,
        session.token,
        config.apiBaseUrl
      )
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
    return withDashboardAuthentication(() =>
      previewHistoricalBackfillData(data, session.token, config.apiBaseUrl)
    )
  })

export const loadDeliveryPlanDatasets = createServerFn({
  method: "POST",
}).handler(async () => {
  const config = getDashboardConfig()
  const session = await requireDashboardSession()
  markPrivateResponse()
  return withDashboardAuthentication(() =>
    fetchDeliveryPlanDatasetsData(session.token, config.apiBaseUrl)
  )
})

export const loadDeliveryResource = createServerFn({ method: "POST" })
  .validator(deliveryResourceRequestSchema)
  .handler(async ({ data }) => {
    const config = getDashboardConfig()
    const session = await requireDashboardSession()
    if (data.resource !== "alerts" && session.user.role === "viewer")
      throw new Error("沒有執行此操作的權限。")
    markPrivateResponse()
    return withDashboardAuthentication(() =>
      fetchDeliveryResourceData(data, session.token, config.apiBaseUrl)
    )
  })
