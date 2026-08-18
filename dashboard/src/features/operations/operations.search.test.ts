import { describe, expect, it } from "vitest"

import {
  correctionsSearchSchema,
  deliveriesSearchSchema,
  operationsAuditFromSearch,
  qualitySearchSchema,
  rawPayloadAuditFromSearch,
  rawPayloadsSearchSchema,
} from "./operations.search"

describe("operations route search", () => {
  it("uses each route's existing page-size default", () => {
    expect(deliveriesSearchSchema.parse({})).toEqual({ p: 1, ps: 100 })
    expect(qualitySearchSchema.parse({})).toEqual({ p: 1, ps: 25 })
    expect(correctionsSearchSchema.parse({})).toEqual({ p: 1, ps: 50 })
  })

  it("canonicalizes invalid pagination values", () => {
    expect(qualitySearchSchema.parse({ p: "-2", ps: "17" })).toEqual({
      p: 1,
      ps: 25,
    })
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
})
