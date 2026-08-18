import { createFileRoute } from "@tanstack/react-router"

import { CorrectionsPage } from "../../features/operations/operations.corrections"
import {
  correctionsSearchSchema,
  type OperationsPageSearch,
} from "../../features/operations/operations.search"

export const Route = createFileRoute("/_authenticated/operations/corrections")({
  validateSearch: correctionsSearchSchema,
  component: CorrectionsRoute,
})

function CorrectionsRoute() {
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  return (
    <CorrectionsPage
      search={search}
      updateSearch={(next: OperationsPageSearch) =>
        void navigate({ search: next, replace: true })
      }
    />
  )
}
