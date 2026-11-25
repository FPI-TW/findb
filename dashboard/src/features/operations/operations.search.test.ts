import { describe, expect, it } from "vitest"

import {
  completenessSearchSchema,
  correctionsSearchSchema,
  deliveriesSearchSchema,
  operationsAuditFromSearch,
  qualitySearchSchema,
  rawPayloadAuditFromSearch,
  rawPayloadsSearchSchema,
} from "./operations.search"

describe("operations route search", () => {
  it.each(["pilot", "full_market"])(
    "drops the retired profile=%s deep-link parameter",
    profile => {
      expect(deliveriesSearchSchema.parse({ profile })).not.toHaveProperty(
        "profile"
      )
    }
  )

  it("uses each route's existing page-size default", () => {
    expect(deliveriesSearchSchema.parse({})).toEqual({
      p: 1,
      ps: 100,
      tab: "alerts",
      seed: "",
      dataset: "",
      date: "",
      pp: 1,
      pps: 25,
      cp: 1,
      cps: 25,
      bp: 1,
      bps: 25,
    })
    expect(qualitySearchSchema.parse({})).toEqual({ p: 1, ps: 25 })
    expect(correctionsSearchSchema.parse({})).toEqual({ p: 1, ps: 50 })
  })

  it("canonicalizes invalid pagination values", () => {
    expect(qualitySearchSchema.parse({ p: "-2", ps: "17" })).toEqual({
      p: 1,
      ps: 25,
    })
    expect(deliveriesSearchSchema.parse({ cp: "3", cps: "50" })).toEqual(
      expect.objectContaining({ cp: 3, cps: 50 })
    )
  })

  it("validates raw audit filters and maps URL keys to the server contract", () => {
    const parsed = rawPayloadsSearchSchema.parse({
      dataset: "tw_equity_eod",
      run: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
      from: "2026-08-01",
      to: "2026-08-18",
      p: "3",
      ps: "50",
    })

    expect(rawPayloadAuditFromSearch(parsed)).toEqual({
      datasetKey: "tw_equity_eod",
      runId: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
      dateFrom: "2026-08-01",
      dateTo: "2026-08-18",
      page: 3,
      pageSize: 50,
    })
  })

  it("maps simple page state without leaking filters between routes", () => {
    expect(operationsAuditFromSearch({ p: 4, ps: 100 })).toEqual({
      datasetKey: "",
      runId: "",
      dateFrom: "",
      dateTo: "",
      page: 4,
      pageSize: 100,
    })
  })

  it("keeps the matrix range, filters and selected cell", () => {
    expect(completenessSearchSchema.parse({})).toEqual({
      from: "",
      to: "",
      market: "",
      gaps: false,
      dataset: "",
      provider: "",
      date: "",
    })
    expect(
      completenessSearchSchema.parse({
        from: "2026-11-01",
        to: "not-a-date",
        gaps: true,
        dataset: "tw_equity_eod",
        provider: "finlab",
        date: "2026-11-24",
      })
    ).toEqual({
      from: "2026-11-01",
      to: "",
      market: "",
      gaps: true,
      dataset: "tw_equity_eod",
      provider: "finlab",
      date: "2026-11-24",
    })
  })

  it("accepts only a well-formed backfill seed from the matrix", () => {
    expect(
      deliveriesSearchSchema.parse({ seed: "finlab|tw_equity_eod|2026-11-24" })
        .seed
    ).toBe("finlab|tw_equity_eod|2026-11-24")
    expect(
      deliveriesSearchSchema.parse({ seed: "finlab|<x>|today" }).seed
    ).toBe("")
  })
})
