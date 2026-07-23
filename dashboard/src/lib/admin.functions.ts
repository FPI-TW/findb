import { createServerFn } from "@tanstack/react-start"
import { setResponseHeader } from "@tanstack/react-start/server"

import { dashboardRequestSchema } from "./admin-api"
import { fetchDashboardData } from "./admin.server"

export const loadDashboard = createServerFn({ method: "POST" })
  .validator(dashboardRequestSchema)
  .handler(async ({ data }) => {
    setResponseHeader("Cache-Control", "no-store")
    return fetchDashboardData(data, process.env.FINDB_API_BASE_URL)
  })
