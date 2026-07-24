import { createFileRoute } from "@tanstack/react-router"

import { QualityPage } from "../../features/operations/OperationsConsole"

export const Route = createFileRoute("/_authenticated/operations/quality")({
  component: QualityPage,
})
