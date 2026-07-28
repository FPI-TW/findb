import { createFileRoute, redirect } from "@tanstack/react-router"

import { getSession } from "../lib/auth.functions"
import { requiresPasswordChangeRedirect } from "../lib/admin-permissions"

export const Route = createFileRoute("/_authenticated")({
  beforeLoad: async ({ location }) => {
    const session = await getSession()

    if (!session.authenticated) {
      throw redirect({ to: "/login" })
    }

    if (
      requiresPasswordChangeRedirect(
        session.mustChangePassword,
        location.pathname
      )
    ) {
      throw redirect({ to: "/change-password" })
    }

    return {
      username: session.username,
      role: session.role,
      mustChangePassword: session.mustChangePassword,
    }
  },
})
