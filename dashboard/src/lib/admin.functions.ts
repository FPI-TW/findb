import { createServerFn } from "@tanstack/react-start"
import { setResponseHeader } from "@tanstack/react-start/server"

import { dashboardRequestSchema } from "./admin-api"
import { fetchDashboardData } from "./admin.server"
import { getDashboardConfig, requireDashboardSession } from "./auth.server"

export const loadDashboard = createServerFn({ method: "POST" })
  .validator(dashboardRequestSchema)
  .handler(async ({ data }) => {
    const config = getDashboardConfig()
    const session = await requireDashboardSession()
    setResponseHeader("Cache-Control", "no-store")
    setResponseHeader("Vary", "Cookie")
    return fetchDashboardData(data, session.token, config.apiBaseUrl)
  })
