import {
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseQueryResult,
} from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useServerFn } from "@tanstack/react-start"
import { useEffect } from "react"

import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import {
  mergeDashboardRefresh,
  dashboardPanels,
  type DashboardPanelName,
  type DashboardPanelErrors,
  type DashboardRequest,
  type DashboardResponse,
  type OperationsView,
  type RawPayload,
  type RawPayloadDetailRequest,
  type SchedulerMutationResponse,
} from "../../lib/admin-api"
import { loadDashboard, loadRawPayloadDetail } from "../../lib/admin.functions"
import { isDashboardAuthenticationError } from "../../lib/auth-errors"
import {
  OPERATIONS_OVERVIEW_AUDIT,
  type RawPayloadsSearch,
} from "./operations.search"

import {
  liveQueryOptions,
  manualQueryOptions,
  useOperationsRefresh,
  useRegisterOperationsQuery,
} from "../../components/OperationsRefresh"

type OperationsDashboardKey = readonly [
  "operations",
  "dashboard",
  string,
  OperationsView,
  DashboardRequest["audit"],
]

type RawPayloadDetailKey = readonly [
  "operations",
  "raw-payload-detail",
  string,
  Pick<RawPayloadsSearch, "dataset" | "run" | "from" | "to" | "p" | "ps">,
  string,
]

export type OperationsQueryData = DashboardResponse & {
  /** Errors from individual settled panels, while their last-good value is retained. */
  refreshErrors: DashboardPanelErrors
  sourceUpdatedAt: Partial<Record<DashboardPanelName, number>>
}

export const operationsKeys = {
  root: ["operations"] as const,
  dashboardRoot: ["operations", "dashboard"] as const,
  dashboard: (
    view: OperationsView,
    audit: DashboardRequest["audit"],
    sessionScope = "protected"
  ) => ["operations", "dashboard", sessionScope, view, audit] as const,
  overview: (
    audit: DashboardRequest["audit"] = OPERATIONS_OVERVIEW_AUDIT,
    sessionScope = "protected"
  ) => ["operations", "dashboard", sessionScope, "overview", audit] as const,
  rawPayloadDetail: (
    scope: RawPayloadDetailKey[3],
    rawPayloadId: string,
    sessionScope = "protected"
  ) =>
    [
      "operations",
      "raw-payload-detail",
      sessionScope,
      scope,
      rawPayloadId,
    ] as const,
} as const

function queryMessage(reason: unknown, fallback: string) {
  return reason instanceof Error && reason.message ? reason.message : fallback
}

function dashboardQueryData(
  previous: OperationsQueryData | undefined,
  response: DashboardResponse
): OperationsQueryData {
  const merged = mergeDashboardRefresh(previous ?? null, response)
  return {
    ...merged.data,
    refreshErrors: merged.errors,
    sourceUpdatedAt: Object.fromEntries(
      dashboardPanels(response).map(([name, result]) => [
        name,
        result.ok ? Date.now() : (previous?.sourceUpdatedAt?.[name] ?? 0),
      ])
    ),
  }
}

export type OperationsDashboardQuery = UseQueryResult<
  OperationsQueryData,
  Error
> & {
  queryKey: OperationsDashboardKey
}

/**
 * Shared query implementation for every Operations page. React Query owns
 * request de-duplication and key isolation; there is deliberately no route
 * pathname or in-flight ref in this layer.
 */
export function useOperationsDashboardQuery(
  view: OperationsView,
  audit: DashboardRequest["audit"]
): OperationsDashboardQuery {
  const load = useServerFn(loadDashboard)
  const navigate = useOperationsNavigate()
  const queryClient = useQueryClient()
  const sessionScope = useProtectedQueryScope()
  const queryKey = operationsKeys.dashboard(view, audit, sessionScope)

  useRegisterOperationsQuery(queryKey)
  const live =
    view === "overview" || view === "deliveries" || view === "quality"
  const query = useQuery<OperationsQueryData, Error>({
    queryKey,
    queryFn: async () => {
      const response = await load({ data: { view, audit } })
      const previous = queryClient.getQueryData<OperationsQueryData>(queryKey)
      return dashboardQueryData(previous, response)
    },
    ...(live ? liveQueryOptions : manualQueryOptions),
  })

  useEffect(() => {
    if (query.error && isDashboardAuthenticationError(query.error)) {
      void Promise.resolve(navigate({ to: "/login", replace: true })).finally(
        () => {
          queryClient.removeQueries({ queryKey: operationsKeys.root })
        }
      )
    }
  }, [navigate, query.error, queryClient])

  return { ...query, queryKey }
}

export function useOperationsIsFetching() {
  return useOperationsRefresh().pending
}

export function useOperationsDashboardRefresh() {
  return useOperationsRefresh().refresh
}

export function useOperationsDashboardState(query: OperationsDashboardQuery) {
  const response = query.data ?? null
  const errors = query.data?.refreshErrors ?? {}
  const fatalError = query.error
    ? queryMessage(query.error, "無法連線至 FinDB API。")
    : ""
  const initialLoading = query.isPending && response === null
  const pending = query.isFetching
  const entries = response ? dashboardPanels(response) : []
  const panelResults = entries.map(([, result]) => result)
  const retainedSuccessfulPanels = panelResults.filter(
    result => result.ok
  ).length
  const successfulPanels = fatalError
    ? 0
    : entries.reduce(
        (count, [name, result]) => count + (result.ok && !errors[name] ? 1 : 0),
        0
      )
  const hasRefreshError =
    fatalError !== "" || Object.values(errors).some(Boolean)
  const connectionState: "loading" | "healthy" | "degraded" | "failed" =
    response === null
      ? pending
        ? "loading"
        : "failed"
      : retainedSuccessfulPanels === 0
        ? "failed"
        : hasRefreshError
          ? "degraded"
          : "healthy"

  return {
    response,
    errors,
    fatalError,
    initialLoading,
    pending,
    panelResults,
    successfulPanels,
    connectionState,
    retry: () => {
      void query.refetch({ cancelRefetch: false })
    },
    sourceUpdatedAt: query.data?.sourceUpdatedAt ?? {},
    connectionLabel: {
      loading: "載入中",
      healthy: "連線正常",
      degraded: "部分服務異常",
      failed: "連線失敗",
    }[connectionState],
  }
}

export type OperationsDashboardState = ReturnType<
  typeof useOperationsDashboardState
>

type NavigateForOperations = (options: {
  to: string
  replace?: boolean
}) => Promise<unknown>

/** Kept in one small adapter so query hooks remain easy to mock in tests. */
function useOperationsNavigate(): NavigateForOperations {
  return useNavigate() as NavigateForOperations
}

export function useRawPayloadDetailQuery(
  scope: RawPayloadsSearch,
  rawPayloadId: string,
  enabled: boolean
): UseQueryResult<RawPayload, Error> & { queryKey: RawPayloadDetailKey } {
  const loadDetail = useServerFn(loadRawPayloadDetail)
  const navigate = useOperationsNavigate()
  const queryClient = useQueryClient()
  const sessionScope = useProtectedQueryScope()
  const queryKey = operationsKeys.rawPayloadDetail(
    {
      dataset: scope.dataset,
      run: scope.run,
      from: scope.from,
      to: scope.to,
      p: scope.p,
      ps: scope.ps,
    },
    rawPayloadId,
    sessionScope
  )
  useRegisterOperationsQuery(queryKey, enabled)
  const query = useQuery<RawPayload, Error>({
    queryKey,
    queryFn: () =>
      loadDetail({
        data: { rawPayloadId } satisfies RawPayloadDetailRequest,
      }),
    enabled,
    ...manualQueryOptions,
  })

  useEffect(() => {
    if (query.error && isDashboardAuthenticationError(query.error)) {
      void Promise.resolve(navigate({ to: "/login", replace: true })).finally(
        () => {
          queryClient.removeQueries({ queryKey: operationsKeys.root })
        }
      )
    }
  }, [navigate, query.error, queryClient])

  return { ...query, queryKey }
}

export function updateOverviewSchedulerCache(
  queryClient: QueryClient,
  audit: DashboardRequest["audit"],
  response: SchedulerMutationResponse,
  sessionScope = "protected"
) {
  const queryKey = operationsKeys.overview(audit, sessionScope)
  queryClient.setQueryData<OperationsQueryData>(queryKey, current => {
    if (!current || current.view !== "overview") return current
    const next: OperationsQueryData = { ...current }
    if (current.schedulers.ok) {
      next.schedulers = {
        ok: true,
        data: {
          ...current.schedulers.data,
          data: current.schedulers.data.data.map(scheduler =>
            scheduler.scheduler_key === response.data.scheduler_key
              ? response.data
              : scheduler
          ),
        },
      }
    }
    if (current.freshness.ok) {
      next.freshness = {
        ok: true,
        data: {
          ...current.freshness.data,
          data: current.freshness.data.data.map(summary =>
            summary.scheduler_key === response.data.scheduler_key
              ? { ...summary, ...response.data }
              : summary
          ),
        },
      }
    }
    return next
  })
  return queryClient.invalidateQueries({ queryKey, exact: true })
}

export function operationErrorMessage(reason: unknown, fallback: string) {
  return queryMessage(reason, fallback)
}
