import { type z } from "zod"

import {
  buildAuditSearch,
  correctionsSchema,
  dashboardResponseSchema,
  dqIssuesSchema,
  marketFreshnessSchema,
  missingDeliveriesSchema,
  queueHealthSchema,
  rawPayloadSchema,
  rawPayloadsSchema,
  schedulerMutationResponseSchema,
  schedulersResponseSchema,
  type DashboardRequest,
  type RawPayloadDetailRequest,
  type SchedulerMutationRequest,
  type PanelResult,
} from "./admin-api"
import {
  DashboardAuthenticationError,
  isDashboardAuthenticationError,
} from "./auth-errors"

type FetchImplementation = typeof fetch
const UPSTREAM_TIMEOUT_MS = 10_000

function isTimeoutError(reason: unknown) {
  return (
    typeof reason === "object" &&
    reason !== null &&
    "name" in reason &&
    reason.name === "TimeoutError"
  )
}

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
  sessionToken: string,
  path: string,
  schema: T,
  fetchImplementation: FetchImplementation
): Promise<z.output<T>> {
  const url = new URL(path, baseUrl)
  let response: Response
  try {
    response = await fetchImplementation(url, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${sessionToken}`,
        Accept: "application/json",
      },
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    })
  } catch (reason) {
    if (isTimeoutError(reason)) {
      throw new Error("FinDB API request timed out")
    }
    throw new Error("Unable to reach FinDB API")
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new DashboardAuthenticationError()
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
  sessionToken: string,
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
  const fetchedAt = new Date().toISOString()
  if (data.view === "overview") {
    const [freshness, queue, schedulers] = await Promise.allSettled([
      fetchTarget(
        baseUrl,
        sessionToken,
        "/api/v1/admin/market-freshness",
        marketFreshnessSchema,
        fetchImplementation
      ),
      fetchTarget(
        baseUrl,
        sessionToken,
        "/api/v1/admin/queue/health",
        queueHealthSchema,
        fetchImplementation
      ),
      fetchTarget(
        baseUrl,
        sessionToken,
        "/api/v1/admin/schedulers",
        schedulersResponseSchema,
        fetchImplementation
      ),
    ])
    assertNoAuthenticationFailure([freshness, queue, schedulers])
    return dashboardResponseSchema.parse({
      view: data.view,
      fetchedAt,
      freshness: settled(freshness),
      queue: settled(queue),
      schedulers: settled(schedulers),
    })
  }
  if (data.view === "deliveries") {
    const [deliveries] = await Promise.allSettled([
      fetchTarget(
        baseUrl,
        sessionToken,
        `/api/v1/admin/missing-deliveries?status=open&page=${data.audit.page}&page_size=${data.audit.pageSize}`,
        missingDeliveriesSchema,
        fetchImplementation
      ),
    ])
    assertNoAuthenticationFailure([deliveries])
    return dashboardResponseSchema.parse({
      view: data.view,
      fetchedAt,
      deliveries: settled(deliveries),
    })
  }
  if (data.view === "quality") {
    const [issues] = await Promise.allSettled([
      fetchTarget(
        baseUrl,
        sessionToken,
        `/api/v1/admin/dq-issues?${issuesSearch.toString()}`,
        dqIssuesSchema,
        fetchImplementation
      ),
    ])
    assertNoAuthenticationFailure([issues])
    return dashboardResponseSchema.parse({
      view: data.view,
      fetchedAt,
      issues: settled(issues),
    })
  }
  if (data.view === "corrections") {
    const [corrections] = await Promise.allSettled([
      fetchTarget(
        baseUrl,
        sessionToken,
        `/api/v1/admin/corrections?page=${data.audit.page}&page_size=${data.audit.pageSize}`,
        correctionsSchema,
        fetchImplementation
      ),
    ])
    assertNoAuthenticationFailure([corrections])
    return dashboardResponseSchema.parse({
      view: data.view,
      fetchedAt,
      corrections: settled(corrections),
    })
  }
  auditSearch.set("include_payload", "false")
  const [rawPayloads] = await Promise.allSettled([
    fetchTarget(
      baseUrl,
      sessionToken,
      `/api/v1/admin/raw-payloads?${auditSearch.toString()}`,
      rawPayloadsSchema,
      fetchImplementation
    ),
  ])
  assertNoAuthenticationFailure([rawPayloads])
  return dashboardResponseSchema.parse({
    view: data.view,
    fetchedAt,
    rawPayloads: settled(rawPayloads),
  })
}

function assertNoAuthenticationFailure(
  results: PromiseSettledResult<unknown>[]
) {
  if (
    results.some(
      result =>
        result.status === "rejected" &&
        isDashboardAuthenticationError(result.reason)
    )
  ) {
    throw new DashboardAuthenticationError()
  }
}

export async function fetchRawPayloadDetailData(
  data: RawPayloadDetailRequest,
  sessionToken: string,
  baseUrlValue: string | undefined,
  fetchImplementation: FetchImplementation = fetch
) {
  return fetchTarget(
    safeBaseUrl(baseUrlValue),
    sessionToken,
    `/api/v1/admin/raw-payloads/by-id/${encodeURIComponent(data.rawPayloadId)}`,
    rawPayloadSchema,
    fetchImplementation
  )
}

function safeSchedulerPath(schedulerKey: string) {
  return `/api/v1/admin/schedulers/${encodeURIComponent(schedulerKey)}`
}

export async function patchSchedulerData(
  data: SchedulerMutationRequest,
  sessionToken: string,
  baseUrlValue: string | undefined,
  fetchImplementation: FetchImplementation = fetch
) {
  const baseUrl = safeBaseUrl(baseUrlValue)
  const url = new URL(safeSchedulerPath(data.schedulerKey), baseUrl)
  let response: Response
  try {
    response = await fetchImplementation(url, {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${sessionToken}`,
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        desired_state: data.desiredState,
        expected_revision: data.expectedRevision,
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    })
  } catch (reason) {
    if (isTimeoutError(reason)) {
      throw new Error("FinDB API request timed out")
    }
    throw new Error("Unable to reach FinDB API")
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new DashboardAuthenticationError()
    }
    if (response.status === 409) {
      throw new Error("Scheduler revision is stale; refresh and retry")
    }
    throw new Error(`FinDB API request failed (${response.status})`)
  }
  try {
    return schedulerMutationResponseSchema.parse(await response.json())
  } catch {
    throw new Error("FinDB API returned an unexpected response")
  }
}
