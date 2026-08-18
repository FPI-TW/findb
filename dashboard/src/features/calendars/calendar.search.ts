import { z } from "zod"

const currentYear = new Date().getUTCFullYear()

export const calendarSearchSchema = z.object({
  market: z.string().trim().max(10).catch("").default(""),
  year: z.coerce
    .number()
    .int()
    .min(1900)
    .max(2200)
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
