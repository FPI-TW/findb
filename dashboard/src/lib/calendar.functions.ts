import { createServerFn } from "@tanstack/react-start"
import { z } from "zod"

import {
  calendarApplyRequestSchema,
  calendarJsonPreviewRequestSchema,
  calendarManualEditPayload,
  calendarManualEditSchema,
  calendarPreviewResponseSchema,
  calendarPublishRequestSchema,
  calendarRollbackRequestSchema,
  calendarRevisionResponseSchema,
  calendarSelectionSchema,
  calendarYearResponseSchema,
  calendarMarketSchema,
  calendarImportSchema,
  emptyCalendarYear,
  normalizeCalendarPreview,
  normalizeCalendarYear,
} from "./calendar-api"
import { calendarJsonRequest, calendarRequest } from "./calendar.server"
import { assertSameOrigin } from "./auth.server"

export const loadCalendarMarkets = createServerFn({ method: "GET" }).handler(
  async () =>
    z
      .array(calendarMarketSchema)
      .parse(await calendarRequest("/api/v1/admin/calendars/markets"))
)

export const loadCalendarYear = createServerFn({ method: "GET" })
  .validator(calendarSelectionSchema)
  .handler(async ({ data }) => {
    const markets = z
      .array(calendarMarketSchema)
      .parse(await calendarRequest("/api/v1/admin/calendars/markets"))
    const market = markets.find(item => item.market === data.market)
    if (!market) throw new Error("找不到指定的市場設定。")
    try {
      const payload = await calendarRequest(
        `/api/v1/admin/calendars/days?market=${encodeURIComponent(data.market)}&year=${data.year}`
      )
      return normalizeCalendarYear(
        calendarYearResponseSchema.parse(payload),
        market
      )
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : ""
      if (!message.includes("Managed calendar year not found")) throw reason
      return emptyCalendarYear(market, data.year)
    }
  })

export const loadCalendarImports = createServerFn({ method: "GET" })
  .validator(calendarSelectionSchema)
  .handler(async ({ data }) =>
    z
      .array(calendarImportSchema)
      .parse(
        await calendarRequest(
          `/api/v1/admin/calendars/imports?market=${encodeURIComponent(data.market)}&year=${data.year}`
        )
      )
  )

export const loadCalendarRevisions = createServerFn({ method: "GET" })
  .validator(calendarSelectionSchema)
  .handler(async ({ data }) => {
    const rows = z
      .array(calendarRevisionResponseSchema)
      .parse(
        await calendarRequest(
          `/api/v1/admin/calendars/years?market=${encodeURIComponent(data.market)}`
        )
      )
    return rows.filter(item => item.year === data.year)
  })

export const previewCalendarJson = createServerFn({ method: "POST" })
  .validator(calendarJsonPreviewRequestSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    let parsed: unknown
    try {
      parsed = JSON.parse(data.content)
    } catch {
      throw new Error("JSON 格式無法解析。")
    }
    const canonical = z
      .object({
        coverage_mode: z
          .enum(["exceptions", "full_year"])
          .default("exceptions"),
        days: z
          .array(
            z.object({
              trade_date: z.iso.date(),
              status: z.enum(["open", "closed", "settlement_only"]),
              holiday_name: z.string().max(100).nullable().optional(),
              description: z.string().max(2000).nullable().optional(),
              session_open: z.string().nullable().optional(),
              session_close: z.string().nullable().optional(),
            })
          )
          .max(366),
        source_filename: z.string().max(255).optional(),
      })
      .parse(parsed)
    return normalizeCalendarPreview(
      calendarPreviewResponseSchema.parse(
        await calendarJsonRequest(
          "/api/v1/admin/calendars/imports/json/preview",
          { market: data.market, year: data.year, ...canonical },
          ["owner", "operator"]
        )
      )
    )
  })

export const previewCalendarCsv = createServerFn({ method: "POST" })
  .validator(data => {
    if (!(data instanceof FormData)) throw new Error("Expected CSV form data")
    const market = z.string().trim().min(1).max(10).parse(data.get("market"))
    const year = z.coerce
      .number()
      .int()
      .min(1900)
      .max(2200)
      .parse(data.get("year"))
    const file = data.get("file")
    if (!(file instanceof File) || file.size === 0 || file.size > 1_000_000) {
      throw new Error("CSV 檔案必須介於 1 byte 與 1 MiB 之間")
    }
    return { market, year, file }
  })
  .handler(async ({ data }) => {
    assertSameOrigin()
    const bytes = new Uint8Array(await data.file.arrayBuffer())
    let binary = ""
    for (const byte of bytes) binary += String.fromCharCode(byte)
    const content_base64 = btoa(binary)
    return normalizeCalendarPreview(
      calendarPreviewResponseSchema.parse(
        await calendarJsonRequest(
          "/api/v1/admin/calendars/imports/preview",
          {
            market: data.market,
            year: data.year,
            filename: data.file.name,
            content_base64,
          },
          ["owner", "operator"]
        )
      )
    )
  })

export const applyCalendarPreview = createServerFn({ method: "POST" })
  .validator(calendarApplyRequestSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    return calendarRevisionResponseSchema.parse(
      await calendarJsonRequest(
        `/api/v1/admin/calendars/imports/${data.batch_id}/apply`,
        { expected_revision: data.expected_revision },
        ["owner", "operator"]
      )
    )
  })

export const editCalendarDay = createServerFn({ method: "POST" })
  .validator(calendarManualEditSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const { market, date, body } = calendarManualEditPayload(data)
    return calendarRevisionResponseSchema.parse(
      await calendarJsonRequest(
        `/api/v1/admin/calendars/${encodeURIComponent(market)}/${date}`,
        body,
        ["owner", "operator"],
        "PATCH"
      )
    )
  })

export const publishCalendarYear = createServerFn({ method: "POST" })
  .validator(calendarPublishRequestSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const { market, year, expected_revision } = data
    return calendarRevisionResponseSchema.parse(
      await calendarJsonRequest(
        `/api/v1/admin/calendars/${encodeURIComponent(market)}/${year}/publish`,
        { expected_revision },
        ["owner"]
      )
    )
  })

export const rollbackCalendarYear = createServerFn({ method: "POST" })
  .validator(calendarRollbackRequestSchema)
  .handler(async ({ data }) => {
    assertSameOrigin()
    const { market, year, target_revision, expected_revision } = data
    return calendarRevisionResponseSchema.parse(
      await calendarJsonRequest(
        `/api/v1/admin/calendars/${encodeURIComponent(market)}/${year}/rollback`,
        { target_revision, expected_revision },
        ["owner"]
      )
    )
  })
