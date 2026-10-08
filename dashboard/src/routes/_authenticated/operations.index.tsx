import { createFileRoute } from "@tanstack/react-router"

import { overviewSearchSchema } from "../../features/operations/operations.search"

import { OperationsOverviewPage } from "../../features/operations/operations.overview"

export const Route = createFileRoute("/_authenticated/operations/")({
  validateSearch: overviewSearchSchema,
  component: OperationsOverviewRoute,
})

function OperationsOverviewRoute() {
  const { role } = Route.useRouteContext()
  const { profile } = Route.useSearch()
  const navigate = Route.useNavigate()
  return (
    <OperationsOverviewPage
      role={role}
      profile={profile}
      updateProfile={next =>
        void navigate({ search: previous => ({ ...previous, profile: next }) })
      }
    />
  )
}
