import { describe, expect, it } from "vitest"

import { DASHBOARD_BASE_PATH, DASHBOARD_BASE_URL } from "./paths"
import { Route as RootRoute } from "../routes/__root"

describe("dashboard deployment paths", () => {
  it("uses the deployed subpath as its base URL", () => {
    expect(DASHBOARD_BASE_PATH).toBe("/dashboard")
    expect(DASHBOARD_BASE_URL).toBe("/dashboard/")
  })

  it("configures a root-level not found component", () => {
    expect(RootRoute.options.notFoundComponent).toBeTypeOf("function")
  })
})
