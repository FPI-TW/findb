import { describe, expect, it } from "vitest"

import { Route as RootRoute } from "../routes/__root"

describe("dashboard root route", () => {
  it("configures a root-level not found component", () => {
    expect(RootRoute.options.notFoundComponent).toBeTypeOf("function")
  })
})
