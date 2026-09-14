import { createServerFn } from "@tanstack/react-start"

export function getDashboardEnvironmentLabel(
  environment: string | undefined,
  nodeEnvironment: string | undefined
) {
  switch (environment?.trim().toLowerCase()) {
    case "production":
      return "PRODUCTION"
    case "staging":
      return "STAGING"
    case "local":
      return "LOCAL"
    default:
      return nodeEnvironment === "development" ? "LOCAL" : "UNKNOWN"
  }
}

export const getDashboardEnvironment = createServerFn({
  method: "GET",
}).handler(() =>
  getDashboardEnvironmentLabel(
    process.env.FINDB_DASHBOARD_ENVIRONMENT,
    process.env.NODE_ENV
  )
)
