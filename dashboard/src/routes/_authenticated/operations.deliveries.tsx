import { createFileRoute } from "@tanstack/react-router"

import { DeliveriesPage } from "../../features/operations/OperationsConsole"

export const Route = createFileRoute("/_authenticated/operations/deliveries")({
  component: DeliveriesPage,
})
