import { getDashboardEnvironment } from "../../lib/environment.functions"
import { createFileRoute, redirect } from "@tanstack/react-router"

import { overviewSearchSchema } from "../../features/operations/operations.search"

import { OperationsOverviewPage } from "../../features/operations/operations.overview"

export const Route = createFileRoute("/_authenticated/operations/")({
  validateSearch: overviewSearchSchema,
  beforeLoad: async ({ search }) => {
    if (!search.profile)
      throw redirect({
        to: "/operations",
        search: {
          ...search,
          profile:
            (await getDashboardEnvironment()) === "PRODUCTION"
              ? "full_market"
              : "pilot",
        },
        replace: true,
      })
  },
  component: OperationsOverviewRoute,
})

function OperationsOverviewRoute() {
  const { role } = Route.useRouteContext()
  const { profile } = Route.useSearch()
  const navigate = Route.useNavigate()
  return (
    <OperationsOverviewPage
      role={role}
      profile={profile ?? "pilot"}
      updateProfile={next =>
        void navigate({ search: previous => ({ ...previous, profile: next }) })
      }
    />
  )
}
