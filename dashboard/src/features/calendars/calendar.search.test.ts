import { afterEach, describe, expect, it, vi } from "vitest"

import { calendarSearchSchema } from "./calendar.search"

describe("calendar route search", () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it("canonicalizes invalid filters", async () => {
    // The fallback year is fixed when the module loads, so load it at a known date.
    vi.useFakeTimers({ toFake: ["Date"] })
    vi.setSystemTime(new Date("2031-06-15T00:00:00Z"))
    vi.resetModules()
    const { calendarSearchSchema: schema } = await import("./calendar.search")

    const parsed = schema.parse({
      market: " TW ",
      year: "0",
      month: "13",
      status: "holiday",
      q: 42,
    })

    expect(parsed.market).toBe("TW")
    expect(parsed.year).toBe(2031)
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
