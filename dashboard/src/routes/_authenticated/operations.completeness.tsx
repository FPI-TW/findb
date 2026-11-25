import { createFileRoute } from "@tanstack/react-router"

import { CompletenessPage } from "../../features/operations/operations.completeness"
import { completenessSearchSchema } from "../../features/operations/operations.search"

export const Route = createFileRoute("/_authenticated/operations/completeness")(
  {
    validateSearch: completenessSearchSchema,
    component: CompletenessRoute,
  }
)

function CompletenessRoute() {
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  const { role } = Route.useRouteContext()
  return (
    <CompletenessPage
      search={search}
      role={role}
      updateSearch={next => void navigate({ search: next, resetScroll: false })}
    />
  )
}
