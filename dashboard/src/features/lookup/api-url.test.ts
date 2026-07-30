import { describe, expect, it } from "vitest"

import { resolvePublicApiUrl } from "./api-url"

describe("public API URL", () => {
  it.each(["3000", "3333"])(
    "uses the backend port when Dashboard runs locally on %s",
    port => {
      expect(
        resolvePublicApiUrl("/api/v1/serve/lookup/instruments", {
          hostname: "localhost",
          port,
          protocol: "http:",
        })
      ).toBe("http://localhost:8080/api/v1/serve/lookup/instruments")
    }
  )

  it("keeps deployed requests on the same origin for nginx routing", () => {
    expect(
      resolvePublicApiUrl("/api/v1/serve/lookup/instruments", {
        hostname: "findb-staging.tingfong.com",
        port: "",
        protocol: "https:",
      })
    ).toBe("/api/v1/serve/lookup/instruments")
  })
})
