import { createFileRoute } from "@tanstack/react-router"

import OperationsLayout from "../../features/operations/OperationsConsole"

export const Route = createFileRoute("/_authenticated/operations")({
  component: OperationsPage,
})

function OperationsPage() {
  const { role, username } = Route.useRouteContext()

  return <OperationsLayout username={username} role={role} />
}
