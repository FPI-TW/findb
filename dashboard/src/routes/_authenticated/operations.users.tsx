import { createFileRoute, redirect } from "@tanstack/react-router"

import { UsersPage } from "../../features/governance/UsersPage"
import { canViewUsers } from "../../lib/admin-permissions"

export const Route = createFileRoute("/_authenticated/operations/users")({
  beforeLoad: ({ context }) => {
    if (!canViewUsers(context.role)) {
      throw redirect({ to: "/operations" })
    }
  },
  component: UsersPage,
})
