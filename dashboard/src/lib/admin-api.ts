import { z } from "zod"

const isoDateTime = z.string().datetime({ offset: true })
const nullableDateTime = isoDateTime.nullable()

export const auditFiltersSchema = z.object({
  datasetKey: z.string().trim().max(100).default(""),
  runId: z.union([z.uuid(), z.literal("")]).default(""),
  dateFrom: z.union([z.iso.date(), z.literal("")]).default(""),
  dateTo: z.union([z.iso.date(), z.literal("")]).default(""),
  page: z.number().int().positive().default(1),
  pageSize: z.number().int().min(1).max(100).default(25),
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

export const schedulerDesiredStateSchema = z.enum(["running", "stopped"])
export type SchedulerDesiredState = z.infer<typeof schedulerDesiredStateSchema>

export const canonicalSlotIdSchema = z.enum([
  "western_markets_window",
  "global_markets_window",
  "taiwan_market_window",
  "asia_pacific_markets_window",
])
export type CanonicalSlotId = z.infer<typeof canonicalSlotIdSchema>

export const schedulerSchema = z.object({
  scheduler_key: z.string().trim().min(1).max(200),
  provider: z.string().trim().min(1).max(100),
  dataset_keys: z.array(z.string().trim().min(1).max(200)),
  slot_id: canonicalSlotIdSchema,
  scheduled_local_time: z.string().trim().min(1).max(32),
  timezone: z.string().trim().min(1).max(100),
  desired_state: schedulerDesiredStateSchema,
  observed_state: schedulerDesiredStateSchema,
  revision: z.number().int().nonnegative(),
  last_heartbeat_at: nullableDateTime,
  last_cycle_started_at: nullableDateTime,
  last_cycle_completed_at: nullableDateTime,
  last_error: z.string().nullable(),
  created_at: isoDateTime,
  updated_at: isoDateTime,
  heartbeat_age_seconds: z.number().nonnegative().nullable(),
})
export type Scheduler = z.infer<typeof schedulerSchema>

export const schedulersResponseSchema = z.object({
  success: z.literal(true),
  data: z.array(schedulerSchema),
})
export type SchedulersResponse = z.infer<typeof schedulersResponseSchema>

export const schedulerMutationRequestSchema = z.object({
  schedulerKey: z.string().trim().min(1).max(200),
  desiredState: schedulerDesiredStateSchema,
  expectedRevision: z.number().int().nonnegative(),
})
export type SchedulerMutationRequest = z.infer<
  typeof schedulerMutationRequestSchema
>

export const schedulerMutationResponseSchema = z.object({
  success: z.literal(true),
  data: schedulerSchema,
})
export type SchedulerMutationResponse = z.infer<
  typeof schedulerMutationResponseSchema
>

export const freshnessStatusSchema = z.enum([
  "not_due",
  "fresh",
  "partial",
  "late",
  "failed",
  "never_received",
])

export const marketFreshnessSchema = z.object({
  success: z.literal(true),
  data: z.array(
    z.object({
      market: z.string(),
      scheduler_key: z.string().trim().min(1).max(200),
      provider: z.string().trim().min(1).max(100),
      dataset_keys: z.array(z.string().trim().min(1).max(200)),
      slot_id: canonicalSlotIdSchema,
      scheduled_local_time: z.string().trim().min(1).max(32),
      timezone: z.string().trim().min(1).max(100),
      desired_state: schedulerDesiredStateSchema,
      observed_state: schedulerDesiredStateSchema,
      revision: z.number().int().nonnegative(),
      last_heartbeat_at: nullableDateTime,
      last_cycle_started_at: nullableDateTime,
      last_cycle_completed_at: nullableDateTime,
      last_error: z.string().nullable(),
      heartbeat_age_seconds: z.number().nonnegative().nullable(),
      configuration_status: z.enum(["ready", "error"]),
      configuration_errors: z.array(z.string()),
      status: freshnessStatusSchema,
      expected_data_date: z.iso.date().nullable(),
      coverage_data_date: z.iso.date().nullable(),
      last_fetched_at: nullableDateTime,
      last_successful_update_at: nullableDateTime,
      last_complete_at: nullableDateTime,
      next_scheduled_at: nullableDateTime,
      feed_count: z.number().int().nonnegative(),
      fresh_feed_count: z.number().int().nonnegative(),
      late_feed_count: z.number().int().nonnegative(),
      feeds: z
        .array(
          z.object({
            dataset_key: z.string(),
            source: z.string(),
            schema_id: z.string().nullable(),
            schema_version: z.number().int().positive().nullable(),
            expected_data_date: z.iso.date().nullable(),
            latest_successful_data_date: z.iso.date().nullable(),
            last_fetched_at: nullableDateTime,
            last_completed_at: nullableDateTime,
            last_run_id: z.uuid().nullable(),
            total_records: z.number().int().nonnegative().nullable(),
            success_records: z.number().int().nonnegative().nullable(),
            failed_records: z.number().int().nonnegative().nullable(),
            policy_outcome: z.string().nullable(),
            open_missing_delivery_alert: z.boolean(),
            last_failure_code: z.string().nullable(),
            configuration_error: z.string().nullable(),
            status: freshnessStatusSchema,
          })
        )
        .default([]),
    })
  ),
})
export type MarketFreshnessResponse = z.infer<typeof marketFreshnessSchema>
export type MarketFreshness = MarketFreshnessResponse["data"][number]

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
      source: z.string().nullable().optional().default(null),
      provider: z.string().nullable().optional().default(null),
      dataset_key: z.string().nullable().optional().default(null),
      schema_id: z.string().nullable().optional().default(null),
      schema_version: z
        .number()
        .int()
        .positive()
        .nullable()
        .optional()
        .default(null),
      raw_payload_id: z.uuid().nullable().optional().default(null),
      raw_available: z.boolean().default(false),
      fetched_at: nullableDateTime.optional().default(null),
      request_key: z.string().nullable().optional().default(null),
      batch_data_date: z.iso.date().nullable().optional().default(null),
      policy_detail: z
        .record(z.string(), z.json())
        .nullable()
        .optional()
        .default(null),
      resolved: z.boolean(),
      created_at: isoDateTime,
    })
  ),
  pagination: paginationSchema,
})
export type DQIssue = z.infer<typeof dqIssuesSchema>["data"][number]

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
  freshness: panelResultSchema(marketFreshnessSchema),
  queue: panelResultSchema(queueHealthSchema),
  schedulers: panelResultSchema(schedulersResponseSchema),
  deliveries: panelResultSchema(missingDeliveriesSchema),
  issues: panelResultSchema(dqIssuesSchema),
  corrections: panelResultSchema(correctionsSchema),
  rawPayloads: panelResultSchema(rawPayloadsSchema),
})

export type DashboardResponse = z.infer<typeof dashboardResponseSchema>
export type FreshnessStatus = z.infer<typeof freshnessStatusSchema>
export type PanelResult<T> =
  { ok: true; data: T } | { ok: false; error: string }

export function mergeDashboardRefresh(
  current: DashboardResponse | null,
  next: DashboardResponse
): {
  data: DashboardResponse
  freshnessError: string
  schedulersError: string
} {
  const retainFreshness = !next.freshness.ok && current?.freshness.ok === true
  const retainSchedulers =
    !next.schedulers.ok && current?.schedulers.ok === true
  const nextFreshnessError = next.freshness.ok ? "" : next.freshness.error
  const nextSchedulersError = next.schedulers.ok ? "" : next.schedulers.error
  const freshnessError = retainFreshness ? nextFreshnessError : ""
  const schedulersError = retainSchedulers ? nextSchedulersError : ""
  const data = {
    ...next,
    ...(retainFreshness && current ? { freshness: current.freshness } : {}),
    ...(retainSchedulers && current ? { schedulers: current.schedulers } : {}),
  }
  return {
    data,
    freshnessError: freshnessError || nextFreshnessError,
    schedulersError: schedulersError || nextSchedulersError,
  }
}

export function buildAuditSearch(filters: DashboardRequest["audit"]) {
  const params = new URLSearchParams({
    page: filters.page.toString(),
    page_size: filters.pageSize.toString(),
  })
  if (filters.datasetKey) params.set("dataset_key", filters.datasetKey)
  if (filters.runId) params.set("run_id", filters.runId)
  if (filters.dateFrom) params.set("date_from", filters.dateFrom)
  if (filters.dateTo) params.set("date_to", filters.dateTo)
  return params
}
