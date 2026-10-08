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

/** 403 also represents valid sessions lacking a role or a required password change. */
export async function assertDashboardAuthentication(response: Response) {
  if (response.status === 401) throw new DashboardAuthenticationError()
  if (response.status !== 403) return
  const payload: unknown = await response
    .clone()
    .json()
    .catch(() => null)
  if (
    typeof payload === "object" &&
    payload !== null &&
    "detail" in payload &&
    payload.detail === "Invalid admin credential"
  )
    throw new DashboardAuthenticationError()
}
