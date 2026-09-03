import { z } from "zod"

import type { DashboardRequest } from "../../lib/admin-api"

export const OPERATIONS_PAGE_SIZES = [25, 50, 100] as const

export const OPERATIONS_OVERVIEW_AUDIT = {
  datasetKey: "",
  runId: "",
  dateFrom: "",
  dateTo: "",
  page: 1,
  pageSize: 25,
} as const

function pageSchema(defaultPageSize: (typeof OPERATIONS_PAGE_SIZES)[number]) {
  return z.object({
    p: z.coerce.number().int().positive().catch(1).default(1),
    ps: z.coerce
      .number()
      .refine(
        value =>
          OPERATIONS_PAGE_SIZES.includes(
            value as (typeof OPERATIONS_PAGE_SIZES)[number]
          ),
        "Unsupported page size"
      )
      .catch(defaultPageSize)
      .default(defaultPageSize),
  })
}

export const deliveriesSearchSchema = pageSchema(100).extend({
  bp: z.coerce.number().int().positive().catch(1).default(1),
  bps: z.coerce
    .number()
    .refine(
      value =>
        OPERATIONS_PAGE_SIZES.includes(
          value as (typeof OPERATIONS_PAGE_SIZES)[number]
        ),
      "Unsupported backfill page size"
    )
    .catch(25)
    .default(25),
})
export const qualitySearchSchema = pageSchema(25)
export const correctionsSearchSchema = pageSchema(50)
export const rawPayloadsSearchSchema = pageSchema(25).extend({
  dataset: z.string().trim().max(100).catch("").default(""),
  run: z
    .union([z.uuid(), z.literal("")])
    .catch("")
    .default(""),
  from: z
    .union([z.iso.date(), z.literal("")])
    .catch("")
    .default(""),
  to: z
    .union([z.iso.date(), z.literal("")])
    .catch("")
    .default(""),
})

export type OperationsPageSearch = z.output<typeof qualitySearchSchema>
export type DeliveriesPageSearch = z.output<typeof deliveriesSearchSchema>
export type RawPayloadsSearch = z.output<typeof rawPayloadsSearchSchema>

export function operationsAuditFromSearch(
  search: OperationsPageSearch
): DashboardRequest["audit"] {
  return {
    datasetKey: "",
    runId: "",
    dateFrom: "",
    dateTo: "",
    page: search.p,
    pageSize: search.ps,
  }
}

export function deliveriesAuditFromSearch(
  search: DeliveriesPageSearch
): DashboardRequest["audit"] {
  return {
    ...operationsAuditFromSearch(search),
    backfillPage: search.bp,
    backfillPageSize: search.bps,
  }
}

export function rawPayloadAuditFromSearch(
  search: RawPayloadsSearch
): DashboardRequest["audit"] {
  return {
    datasetKey: search.dataset,
    runId: search.run,
    dateFrom: search.from,
    dateTo: search.to,
    page: search.p,
    pageSize: search.ps,
  }
}
