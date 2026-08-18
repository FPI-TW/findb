import { createFileRoute } from "@tanstack/react-router"

import { RawPayloadsPage } from "../../features/operations/operations.raw-payloads"
import {
  rawPayloadsSearchSchema,
  type RawPayloadsSearch,
} from "../../features/operations/operations.search"

export const Route = createFileRoute("/_authenticated/operations/raw-payloads")(
  {
    validateSearch: rawPayloadsSearchSchema,
    component: RawPayloadsRoute,
  }
)

function RawPayloadsRoute() {
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  return (
    <RawPayloadsPage
      search={search}
      updateSearch={(next: RawPayloadsSearch) =>
        void navigate({ search: next, replace: true })
      }
    />
  )
}
