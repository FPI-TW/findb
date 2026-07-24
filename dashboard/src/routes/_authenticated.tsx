import { createFileRoute, redirect } from "@tanstack/react-router"

import { getSession } from "../lib/auth.functions"

export const Route = createFileRoute("/_authenticated")({
  beforeLoad: async () => {
    const session = await getSession()

    if (!session.authenticated) {
      throw redirect({ to: "/login" })
    }

    return { username: session.username }
  },
})
