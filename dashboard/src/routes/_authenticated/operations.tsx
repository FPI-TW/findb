import { createFileRoute } from "@tanstack/react-router"

import OperationsConsole from "../../features/operations/OperationsConsole"

export const Route = createFileRoute("/_authenticated/operations")({
  component: OperationsPage,
})

function OperationsPage() {
  const { username } = Route.useRouteContext()

  return <OperationsConsole username={username} />
}
