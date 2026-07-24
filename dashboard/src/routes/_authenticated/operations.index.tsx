import { createFileRoute } from "@tanstack/react-router"

import { OperationsOverviewPage } from "../../features/operations/OperationsConsole"

export const Route = createFileRoute("/_authenticated/operations/")({
  component: OperationsOverviewPage,
})
