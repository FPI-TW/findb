import { describe, expect, it } from "vitest"

import { DEFAULT_SEARCH } from "../features/lookup/config"
import { Route } from "./lookup"

describe("lookup route search validation", () => {
  it("resets legacy macro bookmarks before the page receives search state", () => {
    const validateSearch = Route.options.validateSearch

    expect(validateSearch).toBeTypeOf("function")
    expect(
      (validateSearch as (raw: Record<string, unknown>) => unknown)({
        ds: "macro",
        q: "CPI",
        m: "US",
        ac: "macro",
        st: "ALL",
        sb: "name",
        sd: "desc",
        ps: "100",
        p: "4",
        id: "legacy-series-id",
        fq: "monthly",
        src: "legacy",
      })
    ).toEqual(DEFAULT_SEARCH)
  })
})
