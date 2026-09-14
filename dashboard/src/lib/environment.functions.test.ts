import { describe, expect, it } from "vitest"

import { getDashboardEnvironmentLabel } from "./environment.functions"

describe("getDashboardEnvironmentLabel", () => {
  it.each([
    ["production", "production", "PRODUCTION"],
    [" staging ", "production", "STAGING"],
    ["local", "production", "LOCAL"],
    [undefined, "development", "LOCAL"],
    [undefined, "production", "UNKNOWN"],
  ])("labels %s as %s", (environment, nodeEnvironment, expected) => {
    expect(getDashboardEnvironmentLabel(environment, nodeEnvironment)).toBe(
      expected
    )
  })
})
