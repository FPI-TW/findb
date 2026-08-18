import { describe, expect, it } from "vitest"

import { calendarSearchSchema } from "./calendar.search"

describe("calendar route search", () => {
  it("canonicalizes invalid filters", () => {
    const parsed = calendarSearchSchema.parse({
      market: " TW ",
      year: "0",
      month: "13",
      status: "holiday",
      q: 42,
    })

    expect(parsed.market).toBe("TW")
    expect(parsed.year).toBe(new Date().getUTCFullYear())
    expect(parsed.month).toBe("")
    expect(parsed.status).toBe("")
    expect(parsed.q).toBe("")
  })

  it("accepts deep-linkable market, year, and year-view filters", () => {
    expect(
      calendarSearchSchema.parse({
        market: "US",
        year: "2025",
        month: "07",
        status: "closed",
        q: "holiday",
      })
    ).toEqual({
      market: "US",
      year: 2025,
      month: "07",
      status: "closed",
      q: "holiday",
    })
  })
})
