import { createFileRoute } from "@tanstack/react-router"

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
          replace: true,
        })
      }
    />
  )
}
