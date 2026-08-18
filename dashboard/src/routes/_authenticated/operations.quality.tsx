import { createFileRoute } from "@tanstack/react-router"

import { QualityPage } from "../../features/operations/operations.quality"
import {
  qualitySearchSchema,
  type OperationsPageSearch,
} from "../../features/operations/operations.search"

export const Route = createFileRoute("/_authenticated/operations/quality")({
  validateSearch: qualitySearchSchema,
  component: QualityRoute,
})

function QualityRoute() {
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  return (
    <QualityPage
      search={search}
      updateSearch={(next: OperationsPageSearch) =>
        void navigate({ search: next, replace: true })
      }
    />
  )
}
