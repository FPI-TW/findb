import { z } from "zod"

const isoDateTime = z.string().datetime({ offset: true })
const nullableDateTime = isoDateTime.nullable()

export const auditFiltersSchema = z.object({
  datasetKey: z.string().trim().max(100).default(""),
  runId: z.union([z.uuid(), z.literal("")]).default(""),
  dateFrom: z.union([z.iso.date(), z.literal("")]).default(""),
  dateTo: z.union([z.iso.date(), z.literal("")]).default(""),
})

export const dashboardRequestSchema = z.object({
  audit: auditFiltersSchema,
})

export type DashboardRequest = z.infer<typeof dashboardRequestSchema>

const paginationSchema = z.object({
  page: z.number().int().positive(),
  page_size: z.number().int().positive(),
  total_records: z.number().int().nonnegative(),
  total_pages: z.number().int().nonnegative(),
})

export const queueHealthSchema = z.object({
  counts: z.record(z.string(), z.number().int().nonnegative()),
  oldest_queued_at: nullableDateTime,
  oldest_queued_age_seconds: z.number().nonnegative().nullable(),
  unpublished_outbox: z.number().int().nonnegative(),
  oldest_unpublished_outbox_at: nullableDateTime,
  oldest_unpublished_outbox_age_seconds: z.number().nonnegative().nullable(),
  expired_leases: z.number().int().nonnegative(),
  retry_exhausted: z.number().int().nonnegative(),
  last_worker_heartbeat_at: nullableDateTime,
  worker_heartbeat_age_seconds: z.number().nonnegative().nullable(),
  missing_deliveries: z.number().int().nonnegative(),
  oldest_missing_delivery_at: nullableDateTime,
  oldest_missing_delivery_age_seconds: z.number().nonnegative().nullable(),
})

export const missingDeliveriesSchema = z.object({
  data: z.array(
    z.object({
      alert_id: z.uuid(),
      dataset_key: z.string(),
      source: z.string(),
      schema_id: z.string(),
      schema_version: z.number().int(),
      expected_data_date: z.iso.date(),
      status: z.string(),
      first_detected_at: isoDateTime,
      last_detected_at: isoDateTime,
      resolved_at: nullableDateTime,
    })
  ),
  pagination: paginationSchema,
})

export const dqIssuesSchema = z.object({
  data: z.array(
    z.object({
      id: z.uuid(),
      run_id: z.uuid().nullable(),
      instrument_id: z.uuid().nullable(),
      trade_date: z.iso.date().nullable(),
      issue_type: z.string(),
      severity: z.string(),
      description: z.string().nullable(),
      resolved: z.boolean(),
      created_at: isoDateTime,
    })
  ),
  pagination: paginationSchema,
})

export const correctionsSchema = z.object({
  data: z.array(
    z.object({
      id: z.uuid(),
      table_name: z.string(),
      record_id: z.uuid(),
      instrument_id: z.uuid().nullable(),
      trade_date: z.iso.date().nullable(),
      corrected_by: z.string(),
      correction_reason: z.string(),
      created_at: isoDateTime,
    })
  ),
  pagination: paginationSchema,
})

export const rawPayloadsSchema = z.object({
  data: z.array(
    z.object({
      idempotency_key: z.string(),
      run_id: z.uuid(),
      dataset_key: z.string(),
      source: z.string(),
      schema_id: z.string().nullable(),
      schema_version: z.number().int().nullable(),
      request_key: z.string(),
      payload: z.unknown().pipe(z.json()),
      fetched_at: isoDateTime,
      expire_at: isoDateTime,
      created_at: isoDateTime,
    })
  ),
  pagination: paginationSchema,
})

const panelErrorSchema = z.object({
  ok: z.literal(false),
  error: z.string(),
})

function panelResultSchema<T extends z.ZodType>(schema: T) {
  return z.union([
    z.object({ ok: z.literal(true), data: schema }),
    panelErrorSchema,
  ])
}

export const dashboardResponseSchema = z.object({
  fetchedAt: isoDateTime,
  queue: panelResultSchema(queueHealthSchema),
  deliveries: panelResultSchema(missingDeliveriesSchema),
  issues: panelResultSchema(dqIssuesSchema),
  corrections: panelResultSchema(correctionsSchema),
  rawPayloads: panelResultSchema(rawPayloadsSchema),
})

export type DashboardResponse = z.infer<typeof dashboardResponseSchema>
export type PanelResult<T> =
  { ok: true; data: T } | { ok: false; error: string }

export function buildAuditSearch(filters: DashboardRequest["audit"]) {
  const params = new URLSearchParams({ page: "1", page_size: "10" })
  if (filters.datasetKey) params.set("dataset_key", filters.datasetKey)
  if (filters.runId) params.set("run_id", filters.runId)
  if (filters.dateFrom) params.set("date_from", filters.dateFrom)
  if (filters.dateTo) params.set("date_to", filters.dateTo)
  return params
}
