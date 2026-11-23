import { z } from "zod"

const currentYear = new Date().getUTCFullYear()

export const MIN_CALENDAR_YEAR = 1900
export const MAX_CALENDAR_YEAR = 2200

export const calendarSearchSchema = z.object({
  market: z.string().trim().max(10).catch("").default(""),
  year: z.coerce
    .number()
    .int()
    .min(MIN_CALENDAR_YEAR)
    .max(MAX_CALENDAR_YEAR)
    .catch(currentYear)
    .default(currentYear),
  month: z
    .union([
      z.enum([
        "01",
        "02",
        "03",
        "04",
        "05",
        "06",
        "07",
        "08",
        "09",
        "10",
        "11",
        "12",
      ]),
      z.literal(""),
    ])
    .catch("")
    .default(""),
  status: z
    .enum(["open", "closed", "settlement_only", ""])
    .catch("")
    .default(""),
  q: z.string().max(200).catch("").default(""),
})

export type CalendarSearch = z.output<typeof calendarSearchSchema>
