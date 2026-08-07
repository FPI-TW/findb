const DASHBOARD_AUTHENTICATION_ERROR_MESSAGE =
  "Dashboard authentication required"

export class DashboardAuthenticationError extends Error {
  constructor() {
    super(DASHBOARD_AUTHENTICATION_ERROR_MESSAGE)
    this.name = "DashboardAuthenticationError"
  }
}

export function isDashboardAuthenticationError(error: unknown) {
  return (
    error instanceof Error &&
    error.message === DASHBOARD_AUTHENTICATION_ERROR_MESSAGE
  )
}
