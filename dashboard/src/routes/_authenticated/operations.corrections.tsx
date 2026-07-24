import { createFileRoute } from "@tanstack/react-router"

import { CorrectionsPage } from "../../features/operations/OperationsConsole"

export const Route = createFileRoute("/_authenticated/operations/corrections")({
  component: CorrectionsPage,
})
