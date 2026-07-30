import { createFileRoute } from "@tanstack/react-router"

import { CalendarManagementPage } from "../../features/calendars/CalendarManagementPage"

export const Route = createFileRoute("/_authenticated/operations/calendars")({
  component: CalendarsRoute,
})

function CalendarsRoute() {
  const { role } = Route.useRouteContext()
  return <CalendarManagementPage role={role} />
}
