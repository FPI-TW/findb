import { createFileRoute } from "@tanstack/react-router"

import { OperationsOverviewPage } from "../../features/operations/operations.overview"

export const Route = createFileRoute("/_authenticated/operations/")({
  component: OperationsOverviewRoute,
})

function OperationsOverviewRoute() {
  const { role } = Route.useRouteContext()
  return <OperationsOverviewPage role={role} />
}
