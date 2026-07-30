import { describe, expect, it } from "vitest"

import {
  calendarJsonPreviewRequestSchema,
  calendarManualEditPayload,
  calendarManualEditSchema,
  calendarSelectionSchema,
  emptyCalendarYear,
  normalizeCalendarPreview,
  normalizeCalendarYear,
  calendarPreviewResponseSchema,
  calendarYearResponseSchema,
} from "./calendar-api"

describe("calendar API helpers", () => {
  it("bounds market/year selection and manual text mutations", () => {
    expect(
      calendarSelectionSchema.safeParse({ market: "TW", year: 2026 }).success
    ).toBe(true)
    expect(
      calendarSelectionSchema.safeParse({ market: "TW", year: 1800 }).success
    ).toBe(false)
    expect(
      calendarJsonPreviewRequestSchema.safeParse({
        market: "TW",
        year: 2026,
        content: "{}",
      }).success
    ).toBe(true)
    expect(
      calendarManualEditSchema.safeParse({
        market: "TW",
        date: "2026-01-01",
        expected_revision: 2,
        status: "closed",
        name: null,
        description: null,
        session_open: null,
        session_close: null,
        reason: "行政公告",
      }).success
    ).toBe(true)
    expect(
      calendarManualEditSchema.safeParse({
        market: "TW",
        date: "2026-01-01",
        expected_revision: 2,
        status: "closed",
        name: null,
        description: null,
        session_open: null,
        session_close: null,
        reason: "x",
      }).success
    ).toBe(false)
  })

  it("normalizes backend days into an inspectable annual view", () => {
    const value = normalizeCalendarYear(
      calendarYearResponseSchema.parse({
        revision: {
          market: "TW",
          year: 2026,
          revision: 3,
          status: "draft",
          expected_days: 365,
          actual_days: 2,
          source_kind: "csv",
          source_filename: "holiday.csv",
          published_at: null,
          updated_at: "2026-01-01T00:00:00+00:00",
          coverage_complete: false,
        },
        days: [
          {
            market: "TW",
            trade_date: "2026-01-01",
            status: "closed",
            is_open: false,
            holiday_name: "元旦",
            description: "休市",
            session_open: null,
            session_close: null,
            revision: 3,
            source_kind: "csv",
          },
          {
            market: "TW",
            trade_date: "2026-01-02",
            status: "open",
            is_open: true,
            holiday_name: null,
            description: null,
            session_open: "09:00:00",
            session_close: "13:30:00",
            revision: 3,
            source_kind: "csv",
          },
        ],
      }),
      {
        market: "TW",
        display_name: "台灣",
        timezone: "Asia/Taipei",
        weekend_days: [5, 6],
        default_session_open: "09:00:00",
        default_session_close: "13:30:00",
        active: true,
      }
    )
    expect(value.summary).toMatchObject({
      total: 2,
      open: 1,
      closed: 1,
      settlement_only: 0,
    })
    expect(value.days[1]?.session_close).toBe("13:30")
    expect(value.timezone).toBe("Asia/Taipei")
  })

  it("keeps import controls usable before a market year has a first revision", () => {
    const value = emptyCalendarYear(
      {
        market: "TW",
        display_name: "台灣",
        timezone: "Asia/Taipei",
        weekend_days: [5, 6],
        default_session_open: "09:00:00",
        default_session_close: "13:30:00",
        active: true,
      },
      2027
    )
    expect(value).toMatchObject({
      market: "TW",
      year: 2027,
      current_revision: 0,
      coverage_complete: false,
      summary: { total: 365, open: 261, closed: 104 },
    })
    expect(value.days).toHaveLength(365)
    expect(value.days[0]).toMatchObject({
      date: "2027-01-01",
      status: "open",
      session_open: "09:00",
      session_close: "13:30",
      source_kind: "market_default",
    })
  })

  it("maps the manual display name to the backend holiday_name field", () => {
    const payload = calendarManualEditPayload({
      market: "TW",
      date: "2027-01-01",
      expected_revision: 0,
      status: "closed",
      name: "元旦",
      description: null,
      session_open: null,
      session_close: null,
      reason: "公告休市",
    })
    expect(payload.body).toMatchObject({
      holiday_name: "元旦",
      expected_revision: 0,
      reason: "公告休市",
    })
    expect(payload.body).not.toHaveProperty("name")
  })

  it("turns preview messages into safe display issues", () => {
    const preview = normalizeCalendarPreview(
      calendarPreviewResponseSchema.parse({
        batch_id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
        market: "TW",
        year: 2026,
        input_format: "twse_csv",
        detected_encoding: "cp950",
        base_revision: 2,
        summary: { total: 365, open: 243, closed: 120, settlement_only: 2 },
        warnings: ["說明已清理"],
        errors: ["日期重複"],
        days: [],
        expires_at: "2026-01-01T00:00:00+00:00",
      })
    )
    expect(preview.inferred_year).toBe(2026)
    expect(preview.errors[0]?.message).toBe("日期重複")
    expect(preview.warnings).toHaveLength(1)
  })
})
