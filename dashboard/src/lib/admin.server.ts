import { type z } from "zod"

import {
  buildAuditSearch,
  correctionsSchema,
  dashboardResponseSchema,
  dqIssuesSchema,
  missingDeliveriesSchema,
  queueHealthSchema,
  rawPayloadsSchema,
  type DashboardRequest,
  type PanelResult,
} from "./admin-api"

type FetchImplementation = typeof fetch

function safeBaseUrl(value: string | undefined) {
  const configured = value?.trim() || "http://localhost:8080"
  const parsed = new URL(configured)
  if (!["http:", "https:"].includes(parsed.protocol)) {
    throw new Error("FinDB API base URL must use HTTP or HTTPS")
  }
  return parsed
}

async function fetchTarget<T extends z.ZodType>(
  baseUrl: URL,
  apiKey: string,
  path: string,
  schema: T,
  fetchImplementation: FetchImplementation
): Promise<z.output<T>> {
  const url = new URL(path, baseUrl)
  let response: Response
  try {
    response = await fetchImplementation(url, {
      method: "GET",
      headers: { "X-API-Key": apiKey, Accept: "application/json" },
      cache: "no-store",
    })
  } catch {
    throw new Error("Unable to reach FinDB API")
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new Error("Admin API key was rejected")
    }
    throw new Error(`FinDB API request failed (${response.status})`)
  }
  try {
    return schema.parse(await response.json())
  } catch {
    throw new Error("FinDB API returned an unexpected response")
  }
}

function settled<T>(result: PromiseSettledResult<T>): PanelResult<T> {
  if (result.status === "fulfilled") return { ok: true, data: result.value }
  const message =
    result.reason instanceof Error
      ? result.reason.message
      : "FinDB API request failed"
  return { ok: false, error: message }
}

export async function fetchDashboardData(
  data: DashboardRequest,
  apiKey: string,
  baseUrlValue: string | undefined,
  fetchImplementation: FetchImplementation = fetch
) {
  const baseUrl = safeBaseUrl(baseUrlValue)
  const auditSearch = buildAuditSearch(data.audit)
  const issuesSearch = new URLSearchParams({
    resolved: "false",
    page: data.audit.page.toString(),
    page_size: data.audit.pageSize.toString(),
  })
  const [queue, deliveries, issues, corrections, rawPayloads] =
    await Promise.allSettled([
      fetchTarget(
        baseUrl,
        apiKey,
        "/api/v1/admin/queue/health",
        queueHealthSchema,
        fetchImplementation
      ),
      fetchTarget(
        baseUrl,
        apiKey,
        "/api/v1/admin/missing-deliveries?status=open&page=1&page_size=100",
        missingDeliveriesSchema,
        fetchImplementation
      ),
      fetchTarget(
        baseUrl,
        apiKey,
        `/api/v1/admin/dq-issues?${issuesSearch.toString()}`,
        dqIssuesSchema,
        fetchImplementation
      ),
      fetchTarget(
        baseUrl,
        apiKey,
        "/api/v1/admin/corrections?page=1&page_size=50",
        correctionsSchema,
        fetchImplementation
      ),
      fetchTarget(
        baseUrl,
        apiKey,
        `/api/v1/admin/raw-payloads?${auditSearch.toString()}`,
        rawPayloadsSchema,
        fetchImplementation
      ),
    ])

  return dashboardResponseSchema.parse({
    fetchedAt: new Date().toISOString(),
    queue: settled(queue),
    deliveries: settled(deliveries),
    issues: settled(issues),
    corrections: settled(corrections),
    rawPayloads: settled(rawPayloads),
  })
}
