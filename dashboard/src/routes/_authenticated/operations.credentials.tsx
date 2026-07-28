import { createFileRoute } from "@tanstack/react-router"

import { CredentialsPage } from "../../features/governance/CredentialsPage"

export const Route = createFileRoute("/_authenticated/operations/credentials")({
  component: CredentialsRoute,
})

function CredentialsRoute() {
  const { role } = Route.useRouteContext()
  return <CredentialsPage role={role} />
}
