import { getDashboardEnvironment } from "../../lib/environment.functions"
import { createFileRoute, redirect } from "@tanstack/react-router"

import {
  DeliveriesPage,
  type DeliveriesSearchUpdate,
} from "../../features/operations/operations.deliveries"
import {
  deliveriesSearchSchema,
  type DeliveriesPageSearch,
} from "../../features/operations/operations.search"

export const Route = createFileRoute("/_authenticated/operations/deliveries")({
  validateSearch: deliveriesSearchSchema,
  beforeLoad: async ({ context, search }) => {
    if (!search.profile)
      throw redirect({
        to: "/operations/deliveries",
        search: {
          ...search,
          profile:
            (await getDashboardEnvironment()) === "PRODUCTION"
              ? "full_market"
              : "pilot",
        },
        replace: true,
      })
    if (context.role === "viewer" && search.tab === "backfills")
      throw redirect({
        to: "/operations/deliveries",
        search: { ...search, tab: "plans" },
        replace: true,
      })
  },
  component: DeliveriesRoute,
})

function DeliveriesRoute() {
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  const { role } = Route.useRouteContext()
  return (
    <DeliveriesPage
      search={search}
      role={role}
      updateSearch={(next: DeliveriesSearchUpdate) =>
        void navigate({
          search: current =>
            typeof next === "function"
              ? next(current as DeliveriesPageSearch)
              : next,
          resetScroll: false,
        })
      }
    />
  )
}
