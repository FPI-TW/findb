import { z } from "zod"

const isoDateTime = z.string().datetime({ offset: true })
const nullableDateTime = isoDateTime.nullable()

export const calendarDayStatusSchema = z.enum([
  "open",
  "closed",
  "settlement_only",
])
export type CalendarDayStatus = z.infer<typeof calendarDayStatusSchema>

export const calendarMarketSchema = z.object({
  market: z.string().trim().min(1).max(10),
  display_name: z.string().trim().min(1).max(100),
  timezone: z.string().trim().min(1).max(100),
  weekend_days: z.array(z.number().int().min(0).max(6)).max(7).default([5, 6]),
  active: z.boolean().default(true),
  default_session_open: z.string().nullable().optional(),
  default_session_close: z.string().nullable().optional(),
})
export type CalendarMarket = z.infer<typeof calendarMarketSchema>

export const calendarDaySchema = z.object({
  date: z.iso.date(),
  status: calendarDayStatusSchema,
  is_open: z.boolean().optional(),
  name: z.string().nullable().optional(),
  holiday_name: z.string().nullable().optional(),
  description: z.string().nullable().optional(),
  session_open: z.string().nullable().optional(),
  session_close: z.string().nullable().optional(),
  source_kind: z.string().nullable().optional(),
  source_reference: z.string().nullable().optional(),
  changed_from_published: z.boolean().optional(),
})
export type CalendarDay = z.infer<typeof calendarDaySchema>

export const calendarSummarySchema = z.object({
  total: z.number().int().nonnegative(),
  open: z.number().int().nonnegative(),
  closed: z.number().int().nonnegative(),
  settlement_only: z.number().int().nonnegative(),
  warnings: z.number().int().nonnegative().default(0),
  missing: z.number().int().nonnegative().default(0),
  duplicates: z.number().int().nonnegative().default(0),
})
export type CalendarSummary = z.infer<typeof calendarSummarySchema>

const revisionSchema = z.object({
  revision: z.number().int().nonnegative(),
  status: z.enum(["draft", "published"]),
  updated_at: nullableDateTime.optional(),
  updated_by: z.string().nullable().optional(),
  published_at: nullableDateTime.optional(),
  published_by: z.string().nullable().optional(),
})

export const calendarYearSchema = z.object({
  market: z.string().trim().min(1).max(10),
  year: z.number().int().min(1900).max(2200),
  timezone: z.string().trim().min(1).max(100),
  draft_revision: revisionSchema.nullable().optional(),
  published_revision: revisionSchema.nullable().optional(),
  current_revision: z.number().int().nonnegative().default(0),
  coverage_complete: z.boolean().default(false),
  summary: calendarSummarySchema,
  days: z.array(calendarDaySchema).max(366).default([]),
})
export type CalendarYear = z.infer<typeof calendarYearSchema>

export function emptyCalendarYear(
  market: CalendarMarket,
  year: number
): CalendarYear {
  const first = new Date(Date.UTC(year, 0, 1))
  const expectedDays =
    (Date.UTC(year + 1, 0, 1) - first.getTime()) / (24 * 60 * 60 * 1000)
  const days = Array.from({ length: expectedDays }, (_, offset) => {
    const value = new Date(first)
    value.setUTCDate(value.getUTCDate() + offset)
    const isoWeekday = (value.getUTCDay() + 6) % 7
    const isOpen = !market.weekend_days.includes(isoWeekday)
    return {
      date: value.toISOString().slice(0, 10),
      status: isOpen ? ("open" as const) : ("closed" as const),
      is_open: isOpen,
      name: null,
      holiday_name: null,
      description: null,
      session_open: isOpen
        ? (market.default_session_open?.slice(0, 5) ?? null)
        : null,
      session_close: isOpen
        ? (market.default_session_close?.slice(0, 5) ?? null)
        : null,
      source_kind: "market_default",
    }
  })
  const summary = summaryFromDays(days)
  return {
    market: market.market,
    year,
    timezone: market.timezone,
    draft_revision: null,
    published_revision: null,
    current_revision: 0,
    coverage_complete: false,
    summary,
    days,
  }
}

const issueSchema = z.object({
  row: z.number().int().nonnegative().optional(),
  field: z.string().max(100).optional(),
  message: z.string().max(1000),
})

export const calendarPreviewSchema = z.object({
  batch_id: z.uuid(),
  expires_at: isoDateTime,
  parser_id: z.string().max(100),
  detected_encoding: z.string().max(100).nullable().optional(),
  inferred_year: z.number().int().min(1900).max(2200).nullable().optional(),
  base_revision: z.number().int().nonnegative(),
  summary: calendarSummarySchema,
  diff: z.object({
    added: z.number().int().nonnegative(),
    changed: z.number().int().nonnegative(),
    unchanged: z.number().int().nonnegative(),
    conflicts: z.number().int().nonnegative(),
  }),
  errors: z.array(issueSchema).max(500).default([]),
  warnings: z.array(issueSchema).max(500).default([]),
  days: z.array(calendarDaySchema).max(366).default([]),
})
export type CalendarPreview = z.infer<typeof calendarPreviewSchema>

export const calendarImportSchema = z.object({
  id: z.uuid(),
  input_format: z.string().max(40),
  source_filename: z.string().nullable().optional(),
  source_sha256: z.string().nullable().optional(),
  status: z.string().max(40),
  revision: z.number().int().nonnegative().nullable().optional(),
  created_at: isoDateTime,
  created_by: z.string().nullable().optional(),
})
export type CalendarImport = z.infer<typeof calendarImportSchema>

export const calendarSelectionSchema = z.object({
  market: z.string().trim().min(1).max(10),
  year: z.number().int().min(1900).max(2200),
})

export const calendarManualEditSchema = z.object({
  market: z.string().trim().min(1).max(10),
  date: z.iso.date(),
  expected_revision: z.number().int().nonnegative(),
  status: calendarDayStatusSchema,
  name: z.string().trim().max(200).nullable(),
  description: z.string().trim().max(2000).nullable(),
  session_open: z
    .string()
    .regex(/^\d{2}:\d{2}$/)
    .nullable(),
  session_close: z
    .string()
    .regex(/^\d{2}:\d{2}$/)
    .nullable(),
  reason: z.string().trim().min(3).max(500),
})
export type CalendarManualEdit = z.infer<typeof calendarManualEditSchema>

export function calendarManualEditPayload(data: CalendarManualEdit) {
  const { market, date, name, ...body } = data
  return {
    market,
    date,
    body: { ...body, holiday_name: name },
  }
}

export const calendarJsonPreviewRequestSchema = z.object({
  market: z.string().trim().min(1).max(10),
  year: z.number().int().min(1900).max(2200),
  content: z.string().trim().min(2).max(1_000_000),
})

export const calendarApplyRequestSchema = z.object({
  batch_id: z.uuid(),
  expected_revision: z.number().int().nonnegative(),
})

export const calendarPublishRequestSchema = calendarSelectionSchema.extend({
  expected_revision: z.number().int().nonnegative(),
})

export const calendarRollbackRequestSchema = calendarSelectionSchema.extend({
  target_revision: z.number().int().positive(),
})

export function calendarStatusLabel(status: CalendarDayStatus) {
  if (status === "open") return "開市"
  if (status === "closed") return "休市"
  return "僅結算"
}

export function calendarDayName(day: CalendarDay) {
  return day.name ?? day.holiday_name ?? "—"
}

export function buildCalendarSearch(
  selection: z.infer<typeof calendarSelectionSchema>
) {
  return new URLSearchParams({
    market: selection.market,
    year: String(selection.year),
  })
}

// Backend transport schemas. The UI uses the normalized types above so it can
// keep a stable presentation contract while the Admin API remains intentionally
// minimal (revision response versus complete year response).
export const calendarRevisionResponseSchema = z.object({
  market: z.string().trim().min(1).max(10),
  year: z.number().int().min(1900).max(2200),
  revision: z.number().int().nonnegative(),
  status: z.enum(["draft", "published", "superseded"]),
  expected_days: z.number().int().nonnegative(),
  actual_days: z.number().int().nonnegative(),
  source_kind: z.string(),
  source_filename: z.string().nullable().optional(),
  published_at: nullableDateTime.optional(),
  updated_at: isoDateTime,
  coverage_complete: z.boolean(),
})
export type CalendarRevision = z.infer<typeof calendarRevisionResponseSchema>

export const calendarYearResponseSchema = z.object({
  revision: calendarRevisionResponseSchema,
  days: z
    .array(
      z.object({
        market: z.string(),
        trade_date: z.iso.date(),
        status: calendarDayStatusSchema,
        is_open: z.boolean(),
        holiday_name: z.string().nullable(),
        description: z.string().nullable(),
        session_open: z.string().nullable(),
        session_close: z.string().nullable(),
        revision: z.number().int().nonnegative(),
        source_kind: z.string(),
      })
    )
    .max(366),
})

export const calendarPreviewResponseSchema = z.object({
  batch_id: z.uuid(),
  market: z.string(),
  year: z.number().int(),
  input_format: z.string(),
  detected_encoding: z.string().nullable(),
  base_revision: z.number().int().nonnegative(),
  summary: z.record(z.string(), z.number().int().nonnegative()),
  warnings: z.array(z.string()),
  errors: z.array(z.string()),
  days: z
    .array(
      z.object({
        trade_date: z.iso.date(),
        status: calendarDayStatusSchema,
        holiday_name: z.string().nullable(),
        description: z.string().nullable(),
        session_open: z.string().nullable(),
        session_close: z.string().nullable(),
      })
    )
    .max(366),
  expires_at: isoDateTime,
})

export function normalizeCalendarYear(
  payload: z.infer<typeof calendarYearResponseSchema>,
  market: CalendarMarket | undefined
): CalendarYear {
  const revision = payload.revision
  const summary = summaryFromDays(payload.days)
  const detail = {
    revision: revision.revision,
    status:
      revision.status === "published"
        ? ("published" as const)
        : ("draft" as const),
    updated_at: revision.updated_at,
    published_at: revision.published_at,
  }
  return {
    market: revision.market,
    year: revision.year,
    timezone: market?.timezone ?? "UTC",
    draft_revision: revision.status === "draft" ? detail : null,
    published_revision: revision.status === "published" ? detail : null,
    current_revision: revision.revision,
    coverage_complete: revision.coverage_complete,
    summary,
    days: payload.days.map(day => ({
      date: day.trade_date,
      status: day.status,
      is_open: day.is_open,
      holiday_name: day.holiday_name,
      description: day.description,
      session_open: day.session_open?.slice(0, 5) ?? null,
      session_close: day.session_close?.slice(0, 5) ?? null,
      source_kind: day.source_kind,
    })),
  }
}

export function normalizeCalendarPreview(
  payload: z.infer<typeof calendarPreviewResponseSchema>
): CalendarPreview {
  const summary = {
    total: payload.summary.total ?? payload.days.length,
    open: payload.summary.open ?? 0,
    closed: payload.summary.closed ?? 0,
    settlement_only: payload.summary.settlement_only ?? 0,
    warnings: payload.warnings.length,
    missing: payload.summary.missing ?? 0,
    duplicates: payload.summary.duplicates ?? 0,
  }
  return {
    batch_id: payload.batch_id,
    expires_at: payload.expires_at,
    parser_id: payload.input_format,
    detected_encoding: payload.detected_encoding,
    inferred_year: payload.year,
    base_revision: payload.base_revision,
    summary,
    diff: { added: 0, changed: 0, unchanged: 0, conflicts: 0 },
    errors: payload.errors.map(message => ({ message })),
    warnings: payload.warnings.map(message => ({ message })),
    days: payload.days.map(day => ({
      date: day.trade_date,
      status: day.status,
      holiday_name: day.holiday_name,
      description: day.description,
      session_open: day.session_open?.slice(0, 5) ?? null,
      session_close: day.session_close?.slice(0, 5) ?? null,
    })),
  }
}

function summaryFromDays(
  days: Array<{ status: CalendarDayStatus }>
): CalendarSummary {
  return days.reduce<CalendarSummary>(
    (summary, day) => ({
      ...summary,
      total: summary.total + 1,
      [day.status]: summary[day.status] + 1,
    }),
    {
      total: 0,
      open: 0,
      closed: 0,
      settlement_only: 0,
      warnings: 0,
      missing: 0,
      duplicates: 0,
    }
  )
}
