import { createFileRoute } from "@tanstack/react-router"

import { RawPayloadsPage } from "../../features/operations/OperationsConsole"

export const Route = createFileRoute("/_authenticated/operations/raw-payloads")(
  {
    component: RawPayloadsPage,
  }
)
