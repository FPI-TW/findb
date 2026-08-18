import { createFileRoute } from "@tanstack/react-router"
import { useCallback } from "react"

import { CredentialsPage } from "../../features/governance/CredentialsPage"
import type { CredentialFilters } from "../../lib/admin-governance-api"
import { credentialFiltersSchema } from "../../lib/admin-governance-api"

export const Route = createFileRoute("/_authenticated/operations/credentials")({
  validateSearch: credentialFiltersSchema,
  component: CredentialsRoute,
})

function CredentialsRoute() {
  const { role } = Route.useRouteContext()
  const search = Route.useSearch()
  const navigate = Route.useNavigate()
  const updateSearch = useCallback(
    (next: CredentialFilters) => {
      void navigate({ search: next })
    },
    [navigate]
  )
  return (
    <CredentialsPage role={role} search={search} updateSearch={updateSearch} />
  )
}
