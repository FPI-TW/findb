import { createRouter as createTanStackRouter } from "@tanstack/react-router"
import { DASHBOARD_BASE_PATH } from "./lib/paths"
import { routeTree } from "./routeTree.gen"

export function getRouter() {
  const router = createTanStackRouter({
    basepath: DASHBOARD_BASE_PATH,
    routeTree,
    scrollRestoration: true,
    defaultPreload: "intent",
    defaultPreloadStaleTime: 0,
  })

  return router
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof getRouter>
  }
}
