import { createFileRoute } from "@tanstack/react-router"
import { useCallback } from "react"

import { LookupPage } from "../features/lookup/LookupPage"
import type { LookupSearch } from "../features/lookup/types"
import { lookupSearchSchema } from "../features/lookup/utils"

export const Route = createFileRoute("/lookup")({
  validateSearch: lookupSearchSchema,
  component: LookupRoute,
})

function LookupRoute() {
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  const updateSearch = useCallback(
    (next: LookupSearch) => {
      void navigate({ search: next, replace: true })
    },
    [navigate]
  )
  return <LookupPage search={search} updateSearch={updateSearch} />
}
