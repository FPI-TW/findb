import { createFileRoute } from "@tanstack/react-router"

import { CalendarManagementPage } from "../../features/calendars/CalendarManagementPage"
import {
  calendarSearchSchema,
  type CalendarSearch,
} from "../../features/calendars/calendar.search"

export const Route = createFileRoute("/_authenticated/operations/calendars")({
  validateSearch: calendarSearchSchema,
  component: CalendarsRoute,
})

function CalendarsRoute() {
  const { role } = Route.useRouteContext()
  const search = Route.useSearch()
  const navigate = Route.useNavigate()

  function updateSearch(next: CalendarSearch) {
    void navigate({ search: next, replace: true })
  }

  return (
    <CalendarManagementPage
      role={role}
      search={search}
      updateSearch={updateSearch}
    />
  )
}
