import {
  Link,
  Outlet,
  useNavigate,
  useRouterState,
} from "@tanstack/react-router"
import { useServerFn } from "@tanstack/react-start"
import {
  AlertTriangle,
  Archive,
  CalendarClock,
  CheckCircle2,
  Clock3,
  Database,
  Gauge,
  KeyRound,
  LogOut,
  Power,
  RefreshCw,
  Search,
  ShieldCheck,
  TriangleAlert,
  Users,
} from "lucide-react"
import {
  Component,
  createContext,
  Fragment,
  type FormEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react"

import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../../components/ui/alert-dialog"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../../components/ui/card"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { Skeleton } from "../../components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table"
import {
  mergeDashboardRefresh,
  type DashboardRequest,
  type DashboardResponse,
  type DQIssue,
  type FreshnessStatus,
  type MarketFreshness,
  type MarketFreshnessResponse,
  type OperationsView,
  type PanelResult,
  type RawPayload,
  type Scheduler,
  type SchedulerDesiredState,
  type SchedulerMutationResponse,
  type SchedulersResponse,
} from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import { canViewUsers } from "../../lib/admin-permissions"
import {
  loadDashboard,
  loadRawPayloadDetail,
  updateScheduler,
} from "../../lib/admin.functions"
import { isDashboardAuthenticationError } from "../../lib/auth-errors"
import { logout } from "../../lib/auth.functions"
import { toast } from "../../components/ui/toast"

const EMPTY_FILTERS: DashboardRequest["audit"] = {
  datasetKey: "",
  runId: "",
  dateFrom: "",
  dateTo: "",
  page: 1,
  pageSize: 25,
}

export type OperationsContextValue = {
  data: DashboardResponse | null
  error: string
  freshnessError: string
  schedulersError: string
  pending: boolean
  initialLoading: boolean
  role: AdminRole
  applyScheduler: (response: SchedulerMutationResponse) => void
  filters: DashboardRequest["audit"]
  setFilters: (filters: DashboardRequest["audit"]) => void
  refresh: (filters: DashboardRequest["audit"]) => Promise<void>
}

type ViewState<T> = Partial<Record<OperationsView, T>>

export function operationsViewForPathname(
  pathname: string
): OperationsView | null {
  const normalizedPathname = pathname.replace(/\/+$/, "")
  if (normalizedPathname.endsWith("/deliveries")) return "deliveries"
  if (normalizedPathname.endsWith("/quality")) return "quality"
  if (normalizedPathname.endsWith("/corrections")) return "corrections"
  if (normalizedPathname.endsWith("/raw-payloads")) return "rawPayloads"
  if (normalizedPathname.endsWith("/operations")) {
    return "overview"
  }
  return null
}

const POLLING_VIEWS = new Set<OperationsView>([
  "overview",
  "deliveries",
  "quality",
])

export const OperationsContext = createContext<OperationsContextValue | null>(
  null
)

function useOperations() {
  const context = useContext(OperationsContext)
  if (!context) throw new Error("Operations pages must use OperationsLayout")
  return context
}

function formatDate(value: string | null) {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-TW", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Taipei",
  }).format(new Date(value))
}

function formatRelativeDate(value: string | null) {
  if (!value) return ""
  const differenceSeconds = (new Date(value).getTime() - Date.now()) / 1000
  const absoluteSeconds = Math.abs(differenceSeconds)
  const [amount, unit] =
    absoluteSeconds < 60
      ? [differenceSeconds, "second" as const]
      : absoluteSeconds < 3600
        ? [differenceSeconds / 60, "minute" as const]
        : absoluteSeconds < 86_400
          ? [differenceSeconds / 3600, "hour" as const]
          : [differenceSeconds / 86_400, "day" as const]
  return new Intl.RelativeTimeFormat("zh-TW", {
    numeric: "auto",
  }).format(Math.round(amount), unit)
}

function DateWithRelative({ value }: { value: string | null }) {
  if (!value) return <>—</>
  return (
    <span title={value}>
      {formatDate(value)}
      <span className="ml-1 text-xs text-muted">
        （{formatRelativeDate(value)}）
      </span>
    </span>
  )
}

function formatAge(seconds: number | null) {
  if (seconds === null) return "無資料"
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分鐘`
  return `${Math.round(seconds / 3600)} 小時`
}

function formatSchedulerState(value: string) {
  if (value === "running") return "執行中"
  if (value === "stopped") return "已停止"
  if (value === "idle") return "閒置"
  if (value === "unknown") return "未知"
  return value
}

function schedulerStateVariant(value: string) {
  if (value === "running") return "default" as const
  if (value === "stopped" || value === "idle") return "secondary" as const
  return "warning" as const
}

function schedulerIsStale(scheduler: Pick<Scheduler, "heartbeat_age_seconds">) {
  return (
    scheduler.heartbeat_age_seconds === null ||
    scheduler.heartbeat_age_seconds > 90
  )
}

function schedulerErrorMessage(reason: unknown) {
  if (typeof reason === "object" && reason !== null && "status" in reason) {
    const status = (reason as { status?: unknown }).status
    if (status === 409) {
      return "排程版本已被其他使用者更新，請先重新整理後再試。"
    }
  }
  if (reason instanceof Error) {
    if (
      reason.message.toLowerCase().includes("revision") ||
      reason.message.includes("版本") ||
      reason.message.includes("409")
    ) {
      return "排程版本已被其他使用者更新，請先重新整理後再試。"
    }
    return reason.message
  }
  return "排程狀態更新失敗，請稍後再試。"
}

function getVisiblePages(currentPage: number, totalPages: number) {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, index) => index + 1)
  }

  const pages = new Set([
    1,
    totalPages,
    currentPage - 1,
    currentPage,
    currentPage + 1,
  ])
  return [...pages]
    .filter(page => page >= 1 && page <= totalPages)
    .sort((left, right) => left - right)
}

function Metric({
  label,
  value,
  detail,
  wide = false,
}: {
  label: string
  value: ReactNode
  detail?: ReactNode
  wide?: boolean
}) {
  return (
    <Card
      className={
        wide
          ? "col-span-2 gap-0 rounded-lg bg-surface-soft p-3 shadow-none"
          : "gap-0 rounded-lg bg-surface-soft p-3 shadow-none"
      }
    >
      <span className="block text-xs text-muted">{label}</span>
      <strong className="my-1 block font-mono text-xl">{value}</strong>
      {detail && <small className="block text-xs text-muted">{detail}</small>}
    </Card>
  )
}

function Panel({
  title,
  eyebrow,
  icon,
  result,
  loading = false,
  children,
}: {
  title: string
  eyebrow: string
  icon: ReactNode
  result: PanelResult<unknown> | null
  loading?: boolean
  children: ReactNode
}) {
  return (
    <Card className="gap-0 p-5">
      <CardHeader className="mb-4 flex grid-cols-none flex-row items-center gap-3 px-0">
        <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent">
          {icon}
        </span>
        <div>
          <p className="mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase">
            {eyebrow}
          </p>
          <CardTitle asChild className="text-base tracking-tight">
            <h2>{title}</h2>
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent className="px-0">
        {loading ? (
          <LoadingState />
        ) : result?.ok ? (
          children
        ) : (
          <Alert variant="destructive">
            <TriangleAlert size={18} />
            <AlertDescription>
              {result?.error ?? "暫時無法顯示資料"}
            </AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  )
}

function EmptyState({ children }: { children: ReactNode }) {
  return (
    <Alert
      className="min-h-18 place-content-center"
      variant="success"
      role="status"
    >
      <CheckCircle2 size={18} />
      <AlertDescription>{children}</AlertDescription>
    </Alert>
  )
}

function LoadingState({ label = "正在載入資料" }: { label?: string }) {
  return (
    <div
      className="flex min-h-18 flex-col items-stretch justify-center gap-2 rounded-lg bg-surface-soft p-3.5"
      role="status"
      aria-live="polite"
    >
      <span className="sr-only">{label}</span>
      <Skeleton className="h-3 w-2/5 rounded-full" />
      <Skeleton className="h-3 w-full rounded-full" />
      <Skeleton className="h-3 w-3/4 rounded-full" />
    </div>
  )
}

function RefreshStatus({
  pending,
  label,
}: {
  pending: boolean
  label: string
}) {
  return (
    <div className="mb-4 min-h-4" aria-live="polite">
      {pending && (
        <p className="m-0 text-xs text-muted" role="status">
          {label}
        </p>
      )}
    </div>
  )
}

function ConnectionIndicator({
  state,
}: {
  state: "loading" | "healthy" | "degraded" | "failed"
}) {
  if (state === "loading") {
    return (
      <span
        className="size-2 shrink-0 animate-pulse rounded-full bg-muted"
        aria-hidden="true"
      />
    )
  }
  if (state === "healthy") {
    return (
      <span
        className="size-2 shrink-0 rounded-full bg-accent ring-4 ring-accent/15"
        aria-hidden="true"
      />
    )
  }
  if (state === "degraded") {
    return (
      <span
        className="size-2 shrink-0 rounded-full bg-warning ring-4 ring-warning/15"
        aria-hidden="true"
      />
    )
  }
  return (
    <span
      className="size-2 shrink-0 rounded-full bg-danger ring-4 ring-danger/15"
      aria-hidden="true"
    />
  )
}

const navigation = [
  { to: "/operations", label: "導入概況", icon: Gauge, exact: true },
  {
    to: "/operations/deliveries",
    label: "缺漏交付",
    icon: Clock3,
    exact: false,
  },
  {
    to: "/operations/quality",
    label: "資料品質",
    icon: ShieldCheck,
    exact: false,
  },
  {
    to: "/operations/corrections",
    label: "修正稽核",
    icon: Archive,
    exact: false,
  },
  {
    to: "/operations/raw-payloads",
    label: "原始資料",
    icon: Search,
    exact: false,
  },
  {
    to: "/operations/calendars",
    label: "交易日曆",
    icon: CalendarClock,
    exact: false,
  },
  {
    to: "/operations/credentials",
    label: "API Credentials",
    icon: KeyRound,
    exact: false,
  },
  {
    to: "/operations/users",
    label: "管理者使用者",
    icon: Users,
    exact: false,
    ownerOnly: true,
  },
] as const

export default function OperationsLayout({
  username,
  role,
}: {
  username: string
  role: AdminRole
}) {
  const load = useServerFn(loadDashboard)
  const logoutFn = useServerFn(logout)
  const navigate = useNavigate()
  const pathname = useRouterState({ select: state => state.location.pathname })
  const view = operationsViewForPathname(pathname)
  const [dataByView, setDataByView] = useState<ViewState<DashboardResponse>>({})
  const [errorsByView, setErrorsByView] = useState<ViewState<string[]>>({})
  const [fatalErrorsByView, setFatalErrorsByView] = useState<ViewState<string>>(
    {}
  )
  const [pendingByView, setPendingByView] = useState<ViewState<boolean>>({})
  const [filters, setFilters] = useState(EMPTY_FILTERS)
  const dataByViewRef = useRef(dataByView)
  dataByViewRef.current = dataByView
  const filtersRef = useRef(filters)
  filtersRef.current = filters
  const inFlightViews = useRef(new Set<OperationsView>())
  const data = view ? (dataByView[view] ?? null) : null
  const viewErrors = view ? (errorsByView[view] ?? []) : []
  const error = view ? (fatalErrorsByView[view] ?? "") : ""
  const freshnessError = view === "overview" ? (viewErrors[0] ?? "") : ""
  const schedulersError = view === "overview" ? (viewErrors[2] ?? "") : ""
  const pending = view ? (pendingByView[view] ?? data === null) : false
  const applyScheduler = useCallback((response: SchedulerMutationResponse) => {
    const current = dataByViewRef.current.overview
    if (!current || current.view !== "overview") return
    const next = { ...current }
    if (current.schedulers.ok) {
      next.schedulers = {
        ok: true as const,
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
    // Market freshness is the unified overview source.  Keep the card in
    // sync immediately after a successful owner mutation rather than waiting
    // for the next poll; feed freshness fields remain untouched.
    if (current.freshness.ok) {
      next.freshness = {
        ok: true as const,
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
    const nextByView = { ...dataByViewRef.current, overview: next }
    dataByViewRef.current = nextByView
    setDataByView(nextByView)
  }, [])

  const refresh = useCallback(
    async (
      nextFilters: DashboardRequest["audit"],
      requestedView: OperationsView | null = view
    ) => {
      if (!requestedView) return
      if (inFlightViews.current.has(requestedView)) return
      inFlightViews.current.add(requestedView)
      setFatalErrorsByView(current => ({ ...current, [requestedView]: "" }))
      setPendingByView(current => ({ ...current, [requestedView]: true }))
      try {
        const result = await load({
          data: { view: requestedView, audit: nextFilters },
        })
        if (result.view !== requestedView) return
        const merged = mergeDashboardRefresh(
          dataByViewRef.current[requestedView] ?? null,
          result
        )
        const nextByView = {
          ...dataByViewRef.current,
          [requestedView]: merged.data,
        }
        dataByViewRef.current = nextByView
        setDataByView(nextByView)
        setErrorsByView(current => ({
          ...current,
          [requestedView]: merged.errors,
        }))
        setFatalErrorsByView(current => ({ ...current, [requestedView]: "" }))
      } catch (reason) {
        if (isDashboardAuthenticationError(reason)) {
          await navigate({ to: "/login", replace: true })
          return
        }
        setFatalErrorsByView(current => ({
          ...current,
          [requestedView]:
            reason instanceof Error ? reason.message : "無法連線至 FinDB API。",
        }))
      } finally {
        inFlightViews.current.delete(requestedView)
        setPendingByView(current => ({ ...current, [requestedView]: false }))
      }
    },
    [load, navigate, view]
  )

  useEffect(() => {
    if (!view) return
    void refresh(filtersRef.current, view)
    if (!POLLING_VIEWS.has(view)) return
    const interval = window.setInterval(() => {
      void refresh(filtersRef.current, view)
    }, 60_000)
    return () => window.clearInterval(interval)
  }, [refresh, view])

  async function signOut() {
    await logoutFn()
    await navigate({ to: "/login" })
  }

  // Route matching and the nested Outlet can briefly advance on different
  // renders. Until a view has either data or a fatal error, keep its panels in
  // the initial loading state instead of rendering a null-result error.
  const initialLoading = data === null && error === ""
  const panelResults: PanelResult<unknown>[] = data
    ? data.view === "overview"
      ? [data.freshness, data.queue, data.schedulers]
      : data.view === "deliveries"
        ? [data.deliveries]
        : data.view === "quality"
          ? [data.issues]
          : data.view === "corrections"
            ? [data.corrections]
            : [data.rawPayloads]
    : []
  const retainedSuccessfulPanels = panelResults.filter(
    result => result.ok
  ).length
  const successfulPanels = error
    ? 0
    : panelResults.reduce(
        (count, result, index) =>
          count + (result.ok && !viewErrors[index] ? 1 : 0),
        0
      )
  const hasRefreshError =
    error !== "" || viewErrors.some(panelError => panelError !== "")
  const connectionState =
    data === null
      ? pending
        ? "loading"
        : "failed"
      : retainedSuccessfulPanels === 0
        ? "failed"
        : hasRefreshError
          ? "degraded"
          : "healthy"
  const connectionLabel = {
    loading: "載入中",
    healthy: "連線正常",
    degraded: "部分服務異常",
    failed: "連線失敗",
  }[connectionState]

  return (
    <main className="mx-auto w-full max-w-screen-2xl px-3 py-6 sm:px-5 sm:py-9 sm:pb-16">
      <section className="grid grid-cols-1 items-end gap-6 px-1 pt-3 pb-6 lg:grid-cols-2 lg:gap-12">
        <div>
          <p className="mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase">
            FinDB Operations / Governed access
          </p>
          <h1 className="my-2.5 text-4xl leading-none tracking-tighter sm:text-5xl">
            資料導入營運台
          </h1>
          <p className="m-0 max-w-2xl text-base text-muted">
            依資料類別檢查導入穩定度、完整性、品質與稽核紀錄。
          </p>
        </div>
        <Card className="flex-col items-stretch justify-between gap-5 p-4 sm:flex-row sm:items-center">
          <div className="grid gap-1">
            <span className="text-xs text-muted">已登入</span>
            <strong className="font-mono">{username}</strong>
            <small className="text-xs text-muted">{role}</small>
          </div>
          <div className="flex flex-col items-stretch gap-2 sm:flex-row">
            {view && (
              <Button
                type="button"
                onClick={() => void refresh(filters)}
                disabled={pending}
              >
                <RefreshCw
                  className={pending ? "animate-spin" : ""}
                  size={17}
                />
                重新整理
              </Button>
            )}
            <Button
              variant="secondary"
              type="button"
              onClick={() => void signOut()}
            >
              <LogOut size={16} />
              登出
            </Button>
          </div>
        </Card>
      </section>

      {view && error && (
        <Alert className="mb-3" variant="destructive">
          <AlertTriangle size={18} />
          <AlertTitle>無法更新營運資料</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {view && (
        <Alert
          className="mb-5 flex items-center gap-2.5 text-xs text-muted"
          role={initialLoading ? "status" : undefined}
          aria-live="polite"
        >
          <ConnectionIndicator state={connectionState} />
          <strong className="text-ink">{connectionLabel}</strong>
          <span>
            {data
              ? `${successfulPanels}/${panelResults.length} 個資料來源成功 · 最後更新 ${formatDate(data.fetchedAt)}`
              : "正在載入營運資料"}
          </span>
        </Alert>
      )}

      {view && connectionState === "failed" && (
        <Alert className="mb-5" variant="destructive">
          <AlertTriangle size={18} />
          <AlertTitle>Admin API 連線失敗</AlertTitle>
          <AlertDescription>
            所有 Admin API 查詢均失敗，請確認後端服務與 Dashboard server 設定。
          </AlertDescription>
        </Alert>
      )}
      {view && connectionState === "degraded" && (
        <Alert className="mb-5" variant="warning" role="status">
          <AlertTriangle size={18} />
          <AlertTitle>部分服務異常</AlertTitle>
          <AlertDescription>
            部分資料來源暫時無法取得；其餘成功頁面仍為有效結果。
          </AlertDescription>
        </Alert>
      )}

      <div className="grid gap-5 lg:grid-cols-[15rem_minmax(0,1fr)] lg:items-start">
        <aside className="min-w-0 lg:sticky lg:top-21">
          <nav
            className="flex gap-1 overflow-x-auto rounded-xl border border-line bg-surface p-2 shadow-sm lg:flex-col lg:overflow-visible"
            aria-label="營運資料分類"
          >
            {navigation
              .filter(item => !("ownerOnly" in item) || canViewUsers(role))
              .map(item => {
                const Icon = item.icon
                return (
                  <Link
                    key={item.to}
                    to={item.to}
                    activeOptions={{ exact: item.exact }}
                    activeProps={{
                      className:
                        "bg-accent-soft text-accent ring-1 ring-accent/20",
                    }}
                    className="inline-flex min-h-10 shrink-0 items-center gap-2 rounded-lg px-3 text-sm font-bold whitespace-nowrap text-muted transition-colors hover:bg-surface-soft hover:text-ink focus-visible:ring-3 focus-visible:ring-accent/20 focus-visible:outline-none"
                  >
                    <Icon className="size-4" aria-hidden="true" />
                    {item.label}
                  </Link>
                )
              })}
          </nav>
        </aside>
        <OperationsContext.Provider
          value={{
            data,
            error,
            freshnessError,
            schedulersError,
            pending,
            initialLoading,
            role,
            applyScheduler,
            filters,
            setFilters,
            refresh,
          }}
        >
          <div className="min-w-0">
            <Outlet />
          </div>
        </OperationsContext.Provider>
      </div>
    </main>
  )
}

function PageIntro({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string
  title: string
  description: string
}) {
  return (
    <header className="mb-5">
      <p className="mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase">
        {eyebrow}
      </p>
      <h2 className="text-2xl font-bold tracking-tight sm:text-3xl">{title}</h2>
      <p className="mt-2 text-sm text-muted">{description}</p>
    </header>
  )
}

const SLOT_SEMANTIC_LABELS: Record<string, string> = {
  western_markets_window: "西方市場窗口",
  global_markets_window: "全球市場窗口",
  taiwan_market_window: "台灣市場窗口",
  asia_pacific_markets_window: "亞太市場窗口",
}
const STATUS_LABELS: Record<FreshnessStatus, string> = {
  not_due: "尚未到期",
  fresh: "已更新",
  partial: "部分完成",
  late: "延遲",
  failed: "失敗",
  never_received: "從未收到",
}

function freshnessVariant(status: FreshnessStatus) {
  if (status === "fresh") return "default" as const
  if (status === "not_due" || status === "partial") return "warning" as const
  return "destructive" as const
}

function FeedDetails({ feeds }: { feeds: MarketFreshness["feeds"] }) {
  return (
    <div className="mt-3 grid gap-2">
      {feeds.map(feed => (
        <div
          key={`${feed.dataset_key}:${feed.source}:${feed.schema_id}:${feed.schema_version}`}
          className="grid gap-2 rounded-lg border border-line bg-surface p-3 text-xs md:grid-cols-[minmax(0,1fr)_auto]"
        >
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={freshnessVariant(feed.status)}>
                {STATUS_LABELS[feed.status]}
              </Badge>
              <strong className="font-mono wrap-anywhere">
                {feed.dataset_key}
              </strong>
              <span className="text-muted">
                {feed.source} · {feed.schema_id ?? "schema 未設定"}
                {feed.schema_version ? `.v${feed.schema_version}` : ""}
              </span>
            </div>
            <p className="mt-2 mb-0 text-muted">
              資料日 {feed.latest_successful_data_date ?? "—"} / 預期{" "}
              {feed.expected_data_date ?? "—"} · 成功{" "}
              {feed.success_records ?? "—"} / 總數 {feed.total_records ?? "—"} ·
              policy {feed.policy_outcome ?? "—"}
            </p>
            <p className="mt-1 mb-0 text-muted">
              Provider fetched：
              <DateWithRelative value={feed.last_fetched_at} />
              <span className="mx-1">·</span>
              Normalization completed：
              <DateWithRelative value={feed.last_completed_at} />
            </p>
            {feed.last_failure_code && (
              <p className="mt-1 mb-0 text-danger">
                最後失敗：{feed.last_failure_code}
              </p>
            )}
            {feed.configuration_error && (
              <p className="mt-1 mb-0 text-danger">
                設定錯誤：{feed.configuration_error}
              </p>
            )}
          </div>
          <div className="flex items-center justify-end">
            {feed.status !== "fresh" && (
              <Link
                to={
                  feed.open_missing_delivery_alert
                    ? "/operations/deliveries"
                    : "/operations/raw-payloads"
                }
                className="font-bold text-accent underline-offset-4 hover:underline"
              >
                {feed.open_missing_delivery_alert ? "查看缺漏" : "查看稽核"}
              </Link>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

class MarketFreshnessErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (this.state.failed) {
      return (
        <Alert className="mb-5" variant="destructive" role="alert">
          <AlertTriangle size={18} />
          <AlertTitle>市場更新面板無法顯示</AlertTitle>
          <AlertDescription>
            其他營運資料仍可使用；請重新整理後再試。
          </AlertDescription>
        </Alert>
      )
    }
    return this.props.children
  }
}

/** @deprecated The overview uses IngestionOverviewPanel; kept for compatibility. */
export function MarketFreshnessPanel({
  result,
  loading,
  refreshError,
}: {
  result: PanelResult<MarketFreshnessResponse> | null
  loading: boolean
  refreshError: string
}) {
  const summaries = result?.ok ? result.data.data : []
  const slots = [...new Set(summaries.map(summary => summary.slot_id))].sort(
    (left, right) => {
      const leftAuthoritative = summaries
        .filter(summary => summary.slot_id === left)
        .sort(
          (a, b) =>
            a.timezone.localeCompare(b.timezone) ||
            a.scheduled_local_time.localeCompare(b.scheduled_local_time)
        )[0]
      const rightAuthoritative = summaries
        .filter(summary => summary.slot_id === right)
        .sort(
          (a, b) =>
            a.timezone.localeCompare(b.timezone) ||
            a.scheduled_local_time.localeCompare(b.scheduled_local_time)
        )[0]
      if (!leftAuthoritative || !rightAuthoritative) {
        return left.localeCompare(right)
      }
      return (
        leftAuthoritative.timezone.localeCompare(rightAuthoritative.timezone) ||
        leftAuthoritative.scheduled_local_time.localeCompare(
          rightAuthoritative.scheduled_local_time
        ) ||
        left.localeCompare(right)
      )
    }
  )

  return (
    <div className="mb-5">
      <Panel
        eyebrow="Market freshness"
        title="市場資料更新"
        icon={<CalendarClock size={19} />}
        result={result}
        loading={loading}
      >
        {refreshError && result?.ok && (
          <Alert className="mb-4" variant="warning" role="status">
            <AlertTriangle size={18} />
            <AlertTitle>市場更新狀態暫時無法重新取得</AlertTitle>
            <AlertDescription>
              目前保留上次成功資料：{refreshError}
            </AlertDescription>
          </Alert>
        )}
        {result?.ok &&
          (summaries.length === 0 ? (
            <Alert variant="warning">
              <AlertTriangle size={18} />
              <AlertDescription>目前沒有已啟用的市場排程。</AlertDescription>
            </Alert>
          ) : (
            <div className="grid gap-5">
              {slots.map(slotId => {
                const slotMarkets = summaries.filter(
                  summary => summary.slot_id === slotId
                )
                const slotDefinition = [...slotMarkets].sort(
                  (a, b) =>
                    a.timezone.localeCompare(b.timezone) ||
                    a.scheduled_local_time.localeCompare(
                      b.scheduled_local_time
                    ) ||
                    a.market.localeCompare(b.market)
                )[0]
                return (
                  <section key={slotId}>
                    <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
                      <h3 className="font-bold">
                        {SLOT_SEMANTIC_LABELS[slotId] ?? slotId}
                      </h3>
                      <span className="font-mono text-xs text-muted">
                        {slotDefinition
                          ? formatScheduledTime(
                              slotDefinition.scheduled_local_time
                            ) +
                            " · " +
                            slotDefinition.timezone
                          : "—"}{" "}
                        · {slotMarkets.length} 個市場
                      </span>
                    </div>
                    <div className="grid gap-2 xl:grid-cols-2">
                      {slotMarkets.map(summary => (
                        <Card
                          key={`${summary.slot_id}:${summary.market}`}
                          className="gap-3 rounded-xl p-4 shadow-none"
                        >
                          <div className="flex flex-wrap items-start justify-between gap-2">
                            <div>
                              <strong className="text-lg">
                                {summary.market}
                              </strong>
                              <p className="mt-0.5 mb-0 text-xs text-muted">
                                排程 {summary.scheduled_local_time.slice(0, 5)}{" "}
                                · 下次{" "}
                                <DateWithRelative
                                  value={summary.next_scheduled_at}
                                />
                              </p>
                            </div>
                            <Badge variant={freshnessVariant(summary.status)}>
                              {STATUS_LABELS[summary.status]}
                            </Badge>
                          </div>
                          <dl className="grid grid-cols-2 gap-2 text-sm">
                            <div>
                              <dt className="text-xs text-muted">最後成功</dt>
                              <dd className="mt-0.5">
                                <DateWithRelative
                                  value={summary.last_successful_update_at}
                                />
                              </dd>
                            </div>
                            <div>
                              <dt className="text-xs text-muted">完整更新</dt>
                              <dd className="mt-0.5">
                                <DateWithRelative
                                  value={summary.last_complete_at}
                                />
                              </dd>
                            </div>
                            <div>
                              <dt className="text-xs text-muted">
                                涵蓋日 / 預期日
                              </dt>
                              <dd className="mt-0.5 font-mono">
                                {summary.coverage_data_date ?? "—"} /{" "}
                                {summary.expected_data_date ?? "—"}
                              </dd>
                            </div>
                            <div>
                              <dt className="text-xs text-muted">Feed 完成</dt>
                              <dd className="mt-0.5 font-mono">
                                {summary.fresh_feed_count} /{" "}
                                {summary.feed_count}
                                {summary.late_feed_count > 0 &&
                                  ` · ${summary.late_feed_count} 延遲`}
                              </dd>
                            </div>
                          </dl>
                          <details className="rounded-lg bg-surface-soft px-3 py-2">
                            <summary className="cursor-pointer text-xs font-bold text-accent">
                              Feed 明細（{summary.feeds.length}）
                            </summary>
                            <FeedDetails feeds={summary.feeds} />
                          </details>
                        </Card>
                      ))}
                    </div>
                  </section>
                )
              })}
            </div>
          ))}
      </Panel>
    </div>
  )
}

function formatScheduledTime(value: string) {
  const [hour, minute] = value.split(":")
  if (!hour || !minute) return value
  return `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`
}

type SchedulerCardControl = Pick<
  Scheduler,
  | "scheduler_key"
  | "provider"
  | "dataset_keys"
  | "slot_id"
  | "scheduled_local_time"
  | "timezone"
  | "desired_state"
  | "observed_state"
  | "revision"
  | "last_heartbeat_at"
  | "last_cycle_started_at"
  | "last_cycle_completed_at"
  | "last_error"
  | "heartbeat_age_seconds"
>

type IngestionCard = {
  control: SchedulerCardControl
  freshness: MarketFreshness | null
}

function controlFromFreshness(summary: MarketFreshness): SchedulerCardControl {
  return {
    scheduler_key: summary.scheduler_key,
    provider: summary.provider || summary.feeds[0]?.source || "—",
    dataset_keys: summary.dataset_keys,
    slot_id: summary.slot_id,
    scheduled_local_time: summary.scheduled_local_time,
    timezone: summary.timezone,
    desired_state: summary.desired_state,
    observed_state: summary.observed_state,
    revision: summary.revision,
    last_heartbeat_at: summary.last_heartbeat_at,
    last_cycle_started_at: summary.last_cycle_started_at,
    last_cycle_completed_at: summary.last_cycle_completed_at,
    last_error: summary.last_error,
    heartbeat_age_seconds: summary.heartbeat_age_seconds,
  }
}

function buildIngestionCards(
  freshnessResult: PanelResult<MarketFreshnessResponse> | null,
  schedulersResult: PanelResult<SchedulersResponse> | null
): IngestionCard[] {
  const freshnessRows = freshnessResult?.ok ? freshnessResult.data.data : []
  const schedulerRows = schedulersResult?.ok ? schedulersResult.data.data : []
  const schedulerByKey = new Map(
    schedulerRows.map(row => [row.scheduler_key, row] as const)
  )
  const cards = new Map<string, IngestionCard>()

  freshnessRows.forEach(summary => {
    const control =
      schedulerByKey.get(summary.scheduler_key) ?? controlFromFreshness(summary)
    cards.set(control.scheduler_key, { control, freshness: summary })
  })
  schedulerRows.forEach(control => {
    if (!cards.has(control.scheduler_key)) {
      cards.set(control.scheduler_key, { control, freshness: null })
    }
  })
  return [...cards.values()].sort((left, right) =>
    left.control.scheduler_key.localeCompare(right.control.scheduler_key)
  )
}

export type SchedulerRuntimeStatus =
  | "configuration_error"
  | "not_reported"
  | "stale"
  | "stopping"
  | "stopped"
  | "running"
  | "unknown"

type SchedulerRuntimeStatusVariant =
  "default" | "secondary" | "warning" | "destructive"

type SchedulerRuntimeStatusMeta = {
  label: string
  variant: SchedulerRuntimeStatusVariant
}

export const SCHEDULER_RUNTIME_STATUS_META = {
  configuration_error: { label: "設定錯誤", variant: "destructive" },
  not_reported: { label: "尚未回報", variant: "warning" },
  stale: { label: "心跳過期", variant: "warning" },
  stopping: { label: "停止中", variant: "warning" },
  stopped: { label: "停止", variant: "secondary" },
  running: { label: "執行中", variant: "default" },
  unknown: { label: "未知錯誤", variant: "destructive" },
} satisfies Record<SchedulerRuntimeStatus, SchedulerRuntimeStatusMeta>

type SchedulerRuntimeSelectorInput = {
  control: Pick<
    SchedulerCardControl,
    | "desired_state"
    | "observed_state"
    | "heartbeat_age_seconds"
    | "last_heartbeat_at"
  >
  freshness: Pick<
    MarketFreshness,
    "configuration_status" | "configuration_errors"
  > | null
}

/**
 * Select the scheduler's runtime state independently from feed freshness.
 * The order here is intentional: configuration and heartbeat health take
 * precedence before desired/observed state reconciliation.
 */
export function selectSchedulerRuntimeStatus({
  control,
  freshness,
}: SchedulerRuntimeSelectorInput): SchedulerRuntimeStatus {
  if (
    freshness?.configuration_status === "error" ||
    (freshness?.configuration_errors.length ?? 0) > 0
  ) {
    return "configuration_error"
  }

  if (!control.last_heartbeat_at || control.heartbeat_age_seconds === null) {
    return "not_reported"
  }

  if (!Number.isFinite(control.heartbeat_age_seconds)) return "unknown"
  if (control.heartbeat_age_seconds > 90) return "stale"

  if (control.desired_state === "stopped") {
    if (control.observed_state === "running") return "stopping"
    if (control.observed_state === "stopped") return "stopped"
    return "unknown"
  }
  if (control.desired_state === "running") return "running"
  return "unknown"
}

const SCHEDULER_CYCLE_LABELS: Record<Scheduler["observed_state"], string> = {
  running: "Cycle：執行中",
  stopped: "Cycle：閒置",
}

function schedulerCycleLabel(value: Scheduler["observed_state"]) {
  return SCHEDULER_CYCLE_LABELS[value] ?? "Cycle：未知"
}

type IngestionOverviewData = { cards: IngestionCard[] }

function buildIngestionOverviewResult(
  freshnessResult: PanelResult<MarketFreshnessResponse> | null,
  schedulersResult: PanelResult<SchedulersResponse> | null
): PanelResult<IngestionOverviewData> | null {
  if (!freshnessResult && !schedulersResult) return null
  if (freshnessResult?.ok || schedulersResult?.ok) {
    return {
      ok: true,
      data: { cards: buildIngestionCards(freshnessResult, schedulersResult) },
    }
  }
  return {
    ok: false,
    error:
      freshnessResult?.error ??
      schedulersResult?.error ??
      "暫時無法顯示導入概況",
  }
}

type SchedulerMutationTarget = Pick<
  Scheduler,
  "scheduler_key" | "provider" | "desired_state" | "revision"
>

type SchedulerConfirmationTarget = SchedulerMutationTarget

function SchedulerConfirmationDialog({
  target,
  onCancel,
  onConfirm,
}: {
  target: SchedulerConfirmationTarget | null
  onCancel: () => void
  onConfirm: () => void
}) {
  const desiredState =
    target?.desired_state === "running" ? "stopped" : "running"
  const actionLabel = desiredState === "running" ? "啟用" : "停止"

  return (
    <AlertDialog
      open={target !== null}
      onOpenChange={open => {
        if (!open) onCancel()
      }}
    >
      {target && (
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>確認{actionLabel}排程？</AlertDialogTitle>
            <AlertDialogDescription>
              確定要{actionLabel} {target.provider} 的排程「
              {target.scheduler_key}」嗎？
              {desiredState === "running"
                ? "啟用後系統會依照設定時間執行資料抓取。"
                : "停止後系統將不再依排程自動抓取資料。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={onCancel}>取消</AlertDialogCancel>
            <AlertDialogAction
              variant={desiredState === "running" ? "default" : "destructive"}
              onClick={onConfirm}
            >
              確認{actionLabel}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      )}
    </AlertDialog>
  )
}

export function IngestionOverviewPanel({
  freshnessResult,
  schedulersResult,
  loading,
  pending,
  freshnessError,
  schedulersError,
  role,
  applyScheduler: applySchedulerProp,
}: {
  freshnessResult: PanelResult<MarketFreshnessResponse> | null
  schedulersResult: PanelResult<SchedulersResponse> | null
  loading: boolean
  pending: boolean
  freshnessError: string
  schedulersError: string
  role: AdminRole
  applyScheduler?: (response: SchedulerMutationResponse) => void
}) {
  const update = useServerFn(updateScheduler)
  const navigate = useNavigate()
  const context = useContext(OperationsContext)
  const applyScheduler = applySchedulerProp ?? context?.applyScheduler
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(() => new Set())
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({})
  const [confirmationTarget, setConfirmationTarget] =
    useState<SchedulerConfirmationTarget | null>(null)
  const result = buildIngestionOverviewResult(freshnessResult, schedulersResult)
  const cards = result?.ok ? result.data.cards : []

  async function toggleScheduler(control: SchedulerMutationTarget) {
    if (pendingKeys.has(control.scheduler_key)) return
    const desiredState: SchedulerDesiredState =
      control.desired_state === "running" ? "stopped" : "running"
    setPendingKeys(current => {
      const next = new Set(current)
      next.add(control.scheduler_key)
      return next
    })
    setActionErrors(current => {
      const next = { ...current }
      delete next[control.scheduler_key]
      return next
    })
    try {
      const response = await update({
        data: {
          schedulerKey: control.scheduler_key,
          desiredState,
          expectedRevision: control.revision,
        },
      })
      applyScheduler?.(response)
      toast.success(
        `${control.provider} 排程已${desiredState === "running" ? "啟用" : "停止"}`
      )
    } catch (reason) {
      if (isDashboardAuthenticationError(reason)) {
        await navigate({ to: "/login", replace: true })
        return
      }
      const message = schedulerErrorMessage(reason)
      setActionErrors(current => ({
        ...current,
        [control.scheduler_key]: message,
      }))
      toast.error("排程狀態更新失敗", { description: message })
    } finally {
      setPendingKeys(current => {
        const next = new Set(current)
        next.delete(control.scheduler_key)
        return next
      })
    }
  }

  function requestToggleScheduler(control: SchedulerMutationTarget) {
    if (pendingKeys.has(control.scheduler_key)) return
    setConfirmationTarget(control)
  }

  return (
    <Panel
      eyebrow="Ingestion overview"
      title="導入與排程"
      icon={<CalendarClock size={19} />}
      result={result}
      loading={loading}
    >
      {(freshnessError || schedulersError) && result?.ok && (
        <Alert className="mb-4" variant="warning" role="status">
          <AlertTriangle size={18} />
          <AlertTitle>部分導入狀態暫時無法重新取得</AlertTitle>
          <AlertDescription>
            目前保留上次成功資料：
            {[freshnessError, schedulersError].filter(Boolean).join("；")}
          </AlertDescription>
        </Alert>
      )}
      {!loading && (
        <RefreshStatus
          pending={pending}
          label="正在更新導入狀態…目前資料仍可操作。"
        />
      )}
      {result?.ok &&
        (cards.length === 0 ? (
          <Alert variant="warning" role="status">
            <AlertTriangle size={18} />
            <AlertDescription>目前沒有已註冊的資料抓取排程。</AlertDescription>
          </Alert>
        ) : (
          <div className="grid gap-3">
            {cards.map(card => {
              const { control, freshness } = card
              const runtimeStatus = selectSchedulerRuntimeStatus({
                control,
                freshness,
              })
              const runtimeStatusMeta =
                SCHEDULER_RUNTIME_STATUS_META[runtimeStatus]
              const pendingAction = pendingKeys.has(control.scheduler_key)
              const nextState =
                control.desired_state === "running" ? "stopped" : "running"
              const normalizationCompleted =
                freshness?.last_complete_at ??
                freshness?.last_successful_update_at ??
                null
              return (
                <article
                  className="grid gap-4 rounded-xl border border-line bg-surface p-4"
                  key={control.scheduler_key}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="m-0 font-mono text-sm font-bold wrap-anywhere">
                        {control.scheduler_key}
                      </h3>
                      <p className="mt-1 mb-0 text-xs text-muted">
                        Provider：{control.provider} · Dataset：
                        {control.dataset_keys.join(", ") || "—"}
                      </p>
                      <p className="mt-1 mb-0 text-xs text-muted">
                        每日排程{" "}
                        {formatScheduledTime(control.scheduled_local_time)} ·{" "}
                        {control.timezone} · Slot {control.slot_id}
                      </p>
                    </div>
                  </div>

                  <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
                    <div>
                      <dt className="text-xs text-muted">Scheduler 狀態</dt>
                      <dd className="mt-0.5 flex flex-wrap items-center gap-2">
                        <Badge variant={runtimeStatusMeta.variant}>
                          {runtimeStatusMeta.label}
                        </Badge>
                        <span className="text-xs text-muted">
                          {schedulerCycleLabel(control.observed_state)}
                        </span>
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">Provider fetched</dt>
                      <dd className="mt-0.5">
                        <DateWithRelative
                          value={freshness?.last_fetched_at ?? null}
                        />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">
                        Normalization completed
                      </dt>
                      <dd className="mt-0.5">
                        <DateWithRelative value={normalizationCompleted} />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">Heartbeat</dt>
                      <dd className="mt-0.5">
                        {formatAge(control.heartbeat_age_seconds)}
                        <span className="mx-1 text-muted">·</span>
                        <DateWithRelative value={control.last_heartbeat_at} />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">Revision</dt>
                      <dd className="mt-0.5 font-mono">r{control.revision}</dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">最近 cycle 開始</dt>
                      <dd className="mt-0.5">
                        <DateWithRelative
                          value={control.last_cycle_started_at}
                        />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">最近 cycle 完成</dt>
                      <dd className="mt-0.5">
                        <DateWithRelative
                          value={control.last_cycle_completed_at}
                        />
                      </dd>
                    </div>
                    {freshness && (
                      <>
                        <div>
                          <dt className="text-xs text-muted">Feed 完成</dt>
                          <dd className="mt-0.5 font-mono">
                            {freshness.fresh_feed_count} /{" "}
                            {freshness.feed_count}
                            {freshness.late_feed_count > 0 &&
                              ` · ${freshness.late_feed_count} 延遲`}
                          </dd>
                        </div>
                        <div>
                          <dt className="text-xs text-muted">
                            涵蓋日 / 預期日
                          </dt>
                          <dd className="mt-0.5 font-mono">
                            {freshness.coverage_data_date ?? "—"} /{" "}
                            {freshness.expected_data_date ?? "—"}
                          </dd>
                        </div>
                      </>
                    )}
                  </dl>

                  {freshness?.configuration_errors.length ? (
                    <Alert variant="warning" role="status">
                      <TriangleAlert size={17} />
                      <AlertDescription>
                        <span className="font-semibold">設定錯誤：</span>
                        {freshness.configuration_errors.slice(0, 4).join("；")}
                        {freshness.configuration_errors.length > 4 && "；…"}
                      </AlertDescription>
                    </Alert>
                  ) : null}
                  {control.last_error && (
                    <Alert variant="warning" role="status">
                      <TriangleAlert size={17} />
                      <AlertDescription>
                        <span className="font-semibold">最近錯誤：</span>
                        {control.last_error}
                      </AlertDescription>
                    </Alert>
                  )}

                  {freshness && (
                    <details className="rounded-lg bg-surface-soft px-3 py-2">
                      <summary className="cursor-pointer text-xs font-bold text-accent">
                        Feed 明細（{freshness.feeds.length}）
                      </summary>
                      <FeedDetails feeds={freshness.feeds} />
                    </details>
                  )}

                  <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
                    {role === "owner" ? (
                      <Button
                        type="button"
                        variant={
                          control.desired_state === "running"
                            ? "destructive"
                            : "default"
                        }
                        onClick={() => requestToggleScheduler(control)}
                        disabled={pendingAction}
                        aria-busy={pendingAction}
                        aria-label={`${control.provider} 設為${nextState === "running" ? "執行中" : "已停止"}`}
                      >
                        <Power aria-hidden="true" />
                        {pendingAction
                          ? "更新中…"
                          : nextState === "running"
                            ? "啟用排程"
                            : "停止排程"}
                      </Button>
                    ) : (
                      <span className="text-xs text-muted" role="note">
                        唯讀：只有 owner 可以變更排程狀態。
                      </span>
                    )}
                    {actionErrors[control.scheduler_key] && (
                      <p
                        className="m-0 text-xs text-danger"
                        role="alert"
                        aria-live="polite"
                      >
                        {actionErrors[control.scheduler_key]}
                      </p>
                    )}
                  </div>
                </article>
              )
            })}
          </div>
        ))}
      <SchedulerConfirmationDialog
        target={confirmationTarget}
        onCancel={() => setConfirmationTarget(null)}
        onConfirm={() => {
          const target = confirmationTarget
          setConfirmationTarget(null)
          if (target) void toggleScheduler(target)
        }}
      />
    </Panel>
  )
}

export function SchedulerPanel({
  result,
  loading,
  pending,
  refreshError,
  role,
  applyScheduler: applySchedulerProp,
}: {
  result: PanelResult<SchedulersResponse> | null
  loading: boolean
  pending: boolean
  refreshError: string
  role: AdminRole
  applyScheduler?: (response: SchedulerMutationResponse) => void
}) {
  const update = useServerFn(updateScheduler)
  const navigate = useNavigate()
  const context = useContext(OperationsContext)
  const applyScheduler = applySchedulerProp ?? context?.applyScheduler
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(() => new Set())
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({})
  const [confirmationTarget, setConfirmationTarget] =
    useState<SchedulerConfirmationTarget | null>(null)
  const schedulers = result?.ok ? result.data.data : []

  async function toggleScheduler(scheduler: SchedulerMutationTarget) {
    if (pendingKeys.has(scheduler.scheduler_key)) return
    const desiredState: SchedulerDesiredState =
      scheduler.desired_state === "running" ? "stopped" : "running"
    setPendingKeys(current => {
      const next = new Set(current)
      next.add(scheduler.scheduler_key)
      return next
    })
    setActionErrors(current => {
      const next = { ...current }
      delete next[scheduler.scheduler_key]
      return next
    })
    try {
      const response = await update({
        data: {
          schedulerKey: scheduler.scheduler_key,
          desiredState,
          expectedRevision: scheduler.revision,
        },
      })
      applyScheduler?.(response)
      toast.success(
        `${scheduler.provider} 排程已${desiredState === "running" ? "啟用" : "停止"}`
      )
    } catch (reason) {
      if (isDashboardAuthenticationError(reason)) {
        await navigate({ to: "/login", replace: true })
        return
      }
      const message = schedulerErrorMessage(reason)
      setActionErrors(current => ({
        ...current,
        [scheduler.scheduler_key]: message,
      }))
      toast.error("排程狀態更新失敗", { description: message })
    } finally {
      setPendingKeys(current => {
        const next = new Set(current)
        next.delete(scheduler.scheduler_key)
        return next
      })
    }
  }

  function requestToggleScheduler(scheduler: SchedulerMutationTarget) {
    if (pendingKeys.has(scheduler.scheduler_key)) return
    setConfirmationTarget(scheduler)
  }

  return (
    <Panel
      eyebrow="Scheduler control"
      title="資料抓取排程"
      icon={<Power size={19} />}
      result={result}
      loading={loading}
    >
      {!loading && (
        <RefreshStatus
          pending={pending}
          label="正在更新排程狀態…目前資料仍可操作。"
        />
      )}
      {refreshError && result?.ok && (
        <Alert className="mb-4" variant="warning" role="status">
          <AlertTriangle size={18} />
          <AlertTitle>排程狀態暫時無法重新取得</AlertTitle>
          <AlertDescription>
            目前保留上次成功資料：{refreshError}
          </AlertDescription>
        </Alert>
      )}
      {result?.ok &&
        (schedulers.length === 0 ? (
          <Alert variant="warning" role="status">
            <AlertTriangle size={18} />
            <AlertDescription>目前沒有已註冊的資料抓取排程。</AlertDescription>
          </Alert>
        ) : (
          <div className="grid gap-3">
            {schedulers.map(scheduler => {
              const stale = schedulerIsStale(scheduler)
              const pending = pendingKeys.has(scheduler.scheduler_key)
              const nextState =
                scheduler.desired_state === "running" ? "stopped" : "running"
              return (
                <article
                  className="grid gap-4 rounded-xl border border-line bg-surface p-4"
                  key={scheduler.scheduler_key}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="m-0 font-mono text-sm font-bold wrap-anywhere">
                        {scheduler.scheduler_key}
                      </h3>
                      <p className="mt-1 mb-0 text-xs text-muted">
                        Provider：{scheduler.provider} · Dataset：
                        {scheduler.dataset_keys.join(", ") || "—"}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center justify-end gap-1.5">
                      <Badge
                        variant={schedulerStateVariant(scheduler.desired_state)}
                      >
                        期望：{formatSchedulerState(scheduler.desired_state)}
                      </Badge>
                      <Badge
                        variant={schedulerStateVariant(
                          scheduler.observed_state
                        )}
                      >
                        實際：{formatSchedulerState(scheduler.observed_state)}
                      </Badge>
                      {stale && (
                        <Badge variant="destructive">
                          <TriangleAlert aria-hidden="true" />
                          Heartbeat stale
                        </Badge>
                      )}
                    </div>
                  </div>

                  <dl className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
                    <div>
                      <dt className="text-xs text-muted">Heartbeat</dt>
                      <dd className="mt-0.5">
                        {formatAge(scheduler.heartbeat_age_seconds)}
                        <span className="mx-1 text-muted">·</span>
                        <DateWithRelative value={scheduler.last_heartbeat_at} />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">最近 cycle 開始</dt>
                      <dd className="mt-0.5">
                        <DateWithRelative
                          value={scheduler.last_cycle_started_at}
                        />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">最近 cycle 完成</dt>
                      <dd className="mt-0.5">
                        <DateWithRelative
                          value={scheduler.last_cycle_completed_at}
                        />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted">Revision</dt>
                      <dd className="mt-0.5 font-mono">
                        r{scheduler.revision}
                      </dd>
                    </div>
                  </dl>

                  {scheduler.last_error && (
                    <Alert variant="warning" role="status">
                      <TriangleAlert size={17} />
                      <AlertDescription>
                        <span className="font-semibold">最近錯誤：</span>
                        {scheduler.last_error}
                      </AlertDescription>
                    </Alert>
                  )}

                  <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
                    {role === "owner" ? (
                      <Button
                        type="button"
                        variant={
                          scheduler.desired_state === "running"
                            ? "destructive"
                            : "default"
                        }
                        onClick={() => requestToggleScheduler(scheduler)}
                        disabled={pending}
                        aria-busy={pending}
                        aria-label={`${scheduler.provider} 設為${nextState === "running" ? "執行中" : "已停止"}`}
                      >
                        <Power aria-hidden="true" />
                        {pending
                          ? "更新中…"
                          : nextState === "running"
                            ? "啟用排程"
                            : "停止排程"}
                      </Button>
                    ) : (
                      <span className="text-xs text-muted" role="note">
                        唯讀：只有 owner 可以變更排程狀態。
                      </span>
                    )}
                    {actionErrors[scheduler.scheduler_key] && (
                      <p
                        className="m-0 text-xs text-danger"
                        role="alert"
                        aria-live="polite"
                      >
                        {actionErrors[scheduler.scheduler_key]}
                      </p>
                    )}
                  </div>
                </article>
              )
            })}
          </div>
        ))}
      <SchedulerConfirmationDialog
        target={confirmationTarget}
        onCancel={() => setConfirmationTarget(null)}
        onConfirm={() => {
          const target = confirmationTarget
          setConfirmationTarget(null)
          if (target) void toggleScheduler(target)
        }}
      />
    </Panel>
  )
}

export function OperationsOverviewPage() {
  const {
    data,
    freshnessError,
    schedulersError,
    initialLoading,
    pending,
    role,
  } = useOperations()
  const overview = data?.view === "overview" ? data : null
  return (
    <>
      <PageIntro
        eyebrow="Ingestion health"
        title="導入概況"
        description="檢查市場資料更新、佇列、Worker heartbeat 與 outbox 的即時健康狀態。"
      />
      <div className="grid gap-5">
        <MarketFreshnessErrorBoundary key={overview?.fetchedAt ?? "initial"}>
          <IngestionOverviewPanel
            freshnessResult={overview?.freshness ?? null}
            schedulersResult={overview?.schedulers ?? null}
            loading={initialLoading}
            pending={pending}
            freshnessError={freshnessError}
            schedulersError={schedulersError}
            role={role}
          />
        </MarketFreshnessErrorBoundary>
        <Panel
          eyebrow="Queue and worker"
          title="Source ingest 後的佇列與 Worker"
          icon={<Database size={19} />}
          result={overview?.queue ?? null}
          loading={initialLoading}
        >
          {overview?.queue.ok && (
            <>
              <p className="mb-3 text-xs text-muted">
                Source ingest 已寫入 durable queue 後，這裡顯示
                dispatcher、outbox 與 normalization worker 的處理狀態。
              </p>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Metric
                  label="排隊中"
                  value={overview.queue.data.counts.queued ?? 0}
                />
                <Metric
                  label="處理中"
                  value={overview.queue.data.counts.processing ?? 0}
                />
                <Metric
                  label="重試耗盡"
                  value={overview.queue.data.retry_exhausted}
                />
                <Metric
                  label="過期租約"
                  value={overview.queue.data.expired_leases}
                />
                <Metric
                  wide
                  label="Worker heartbeat"
                  value={formatAge(
                    overview.queue.data.worker_heartbeat_age_seconds
                  )}
                  detail={formatDate(
                    overview.queue.data.last_worker_heartbeat_at
                  )}
                />
                <Metric
                  wide
                  label="未發布 outbox"
                  value={overview.queue.data.unpublished_outbox}
                />
              </div>
            </>
          )}
        </Panel>
      </div>
      <Alert className="mt-5" variant="subtle" role="note">
        <AlertTitle>目前限制</AlertTitle>
        <AlertDescription>
          後端尚無歷史 run 趨勢 API，因此本頁只呈現即時佇列狀態。
        </AlertDescription>
      </Alert>
    </>
  )
}

export function DeliveriesPage() {
  const { data, initialLoading } = useOperations()
  const deliveries = data?.view === "deliveries" ? data : null
  return (
    <>
      <PageIntro
        eyebrow="Completeness"
        title="缺漏交付"
        description="追蹤預期日期尚未收到的資料集交付。"
      />
      <Panel
        eyebrow="Open alerts"
        title="未解決的交付缺漏"
        icon={<Clock3 size={19} />}
        result={deliveries?.deliveries ?? null}
        loading={initialLoading}
      >
        {deliveries?.deliveries.ok &&
          (deliveries.deliveries.data.data.length === 0 ? (
            <EmptyState>目前沒有未解決的交付缺漏</EmptyState>
          ) : (
            <>
              <p className="mt-0 mb-2 text-xs text-muted">
                共 {deliveries.deliveries.data.pagination.total_records}{" "}
                筆，顯示前 {deliveries.deliveries.data.data.length} 筆
              </p>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>資料集</TableHead>
                    <TableHead>來源</TableHead>
                    <TableHead>預期日期</TableHead>
                    <TableHead>首次偵測</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {deliveries.deliveries.data.data.map(alert => (
                    <TableRow key={alert.alert_id}>
                      <TableCell className="font-mono wrap-anywhere">
                        {alert.dataset_key}
                      </TableCell>
                      <TableCell>{alert.source}</TableCell>
                      <TableCell>{alert.expected_data_date}</TableCell>
                      <TableCell>
                        {formatDate(alert.first_detected_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </>
          ))}
      </Panel>
    </>
  )
}

function boundedPolicyValue(value: unknown, maxLength = 180) {
  if (value === null || value === undefined) return "—"
  let text: string
  if (typeof value === "string") {
    text = value
  } else if (
    typeof value === "number" ||
    typeof value === "boolean" ||
    typeof value === "bigint"
  ) {
    text = String(value)
  } else {
    try {
      text = JSON.stringify(value)
    } catch {
      text = "[無法顯示]"
    }
  }
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text
}

const POLICY_VIOLATION_FIELDS = [
  "code",
  "action",
  "reason",
  "observed",
  "expected",
] as const

function boundedPolicyField(value: unknown) {
  return typeof value === "object" && value !== null
    ? "[bounded object]"
    : boundedPolicyValue(value)
}

function boundedPolicyViolation(value: unknown) {
  if (value === null || value === undefined) return "—"
  if (typeof value !== "object" || Array.isArray(value)) {
    return boundedPolicyValue(value)
  }
  const record = value as Record<string, unknown>
  const fields = POLICY_VIOLATION_FIELDS.filter(field => field in record)
  if (fields.length === 0) return "[violation detail]"
  return fields
    .map(field => `${field}=${boundedPolicyField(record[field])}`)
    .join(" · ")
}

function DQPolicyDetails({ detail }: { detail: DQIssue["policy_detail"] }) {
  if (!detail) return <span>—</span>
  const violations = Array.isArray(detail.violations) ? detail.violations : []
  const violationCount =
    typeof detail.violation_count === "number"
      ? detail.violation_count
      : typeof detail.count === "number"
        ? detail.count
        : violations.length
  const summaryEntries = POLICY_VIOLATION_FIELDS.filter(key => key in detail)
  const visibleViolations = violations.slice(0, 3)
  const truncated = detail.truncated === true || violations.length > 3

  return (
    <div className="grid max-w-xl gap-1 text-xs">
      <span className="font-semibold">
        {violationCount > 0
          ? `${violationCount} 個 policy violation`
          : "Policy detail"}
        {truncated ? "（truncated／已截斷）" : ""}
      </span>
      {summaryEntries.map(key => (
        <span className="text-muted" key={key}>
          {key}：{boundedPolicyField(detail[key])}
        </span>
      ))}
      {visibleViolations.map((violation, index) => (
        <span
          className="text-muted wrap-anywhere"
          key={`${index}:${boundedPolicyValue(violation, 40)}`}
        >
          violation {index + 1}：{boundedPolicyViolation(violation)}
        </span>
      ))}
      {truncated && visibleViolations.length === 0 && (
        <span className="text-muted">其餘細節未顯示。</span>
      )}
    </div>
  )
}

export function QualityPage() {
  const { data, filters, initialLoading, pending, refresh, setFilters } =
    useOperations()
  const quality = data?.view === "quality" ? data : null
  const issues = quality?.issues.ok ? quality.issues.data : null
  const [pageSizeDraft, setPageSizeDraft] = useState(filters.pageSize)

  function submitIssues(event: FormEvent) {
    event.preventDefault()
    const nextFilters = { ...filters, page: 1, pageSize: pageSizeDraft }
    setFilters(nextFilters)
    void refresh(nextFilters)
  }

  function changePage(page: number) {
    const nextFilters = { ...filters, page }
    setFilters(nextFilters)
    void refresh(nextFilters)
  }

  return (
    <>
      <PageIntro
        eyebrow="Correctness"
        title="資料品質"
        description="集中檢視尚未解決的 DQ 規則問題。"
      />
      <Panel
        eyebrow="Unresolved issues"
        title="未解決 DQ 問題"
        icon={<ShieldCheck size={19} />}
        result={quality?.issues ?? null}
        loading={initialLoading}
      >
        {quality?.issues.ok && (
          <>
            <form
              className="mb-4 flex flex-wrap items-end justify-end gap-2.5"
              onSubmit={submitIssues}
            >
              <div className="grid w-full gap-1.5 sm:w-36">
                <Label htmlFor="quality-page-size">每頁筆數</Label>
                <select
                  id="quality-page-size"
                  className="h-9 w-full rounded-lg border border-line bg-surface px-3 text-sm text-ink outline-none focus-visible:ring-3 focus-visible:ring-accent/20"
                  value={pageSizeDraft}
                  onChange={event =>
                    setPageSizeDraft(Number(event.target.value))
                  }
                  disabled={pending}
                >
                  <option value={25}>25</option>
                  <option value={50}>50</option>
                  <option value={100}>100</option>
                </select>
              </div>
              <Button type="submit" disabled={pending}>
                <Search size={16} /> 查詢
              </Button>
            </form>
            {issues?.data.length === 0 ? (
              <EmptyState>目前沒有未解決的資料品質問題</EmptyState>
            ) : (
              <>
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
                  <p className="m-0">
                    第 {issues?.pagination.page} /{" "}
                    {issues?.pagination.total_pages} 頁，共{" "}
                    {issues?.pagination.total_records} 筆，本頁{" "}
                    {issues?.data.length} 筆
                  </p>
                  {pending && (
                    <span role="status" aria-live="polite">
                      正在更新…
                    </span>
                  )}
                </div>
                <Table scrollMode="page">
                  <TableHeader>
                    <TableRow>
                      <TableHead>嚴重度</TableHead>
                      <TableHead>類型</TableHead>
                      <TableHead>來源 / Dataset / Run</TableHead>
                      <TableHead>批次 / Schema</TableHead>
                      <TableHead>Policy detail</TableHead>
                      <TableHead>說明</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {issues?.data.map(issue => (
                      <TableRow key={issue.id}>
                        <TableCell>
                          <Badge
                            variant={
                              issue.severity === "error"
                                ? "destructive"
                                : "warning"
                            }
                          >
                            {issue.severity}
                          </Badge>
                        </TableCell>
                        <TableCell className="font-mono wrap-anywhere">
                          {issue.issue_type}
                        </TableCell>
                        <TableCell className="whitespace-normal">
                          <div className="grid gap-1 text-xs">
                            <span>
                              Provider：{issue.provider ?? "—"}
                              {issue.source ? ` · Source：${issue.source}` : ""}
                            </span>
                            <span className="font-mono wrap-anywhere">
                              Dataset：{issue.dataset_key ?? "—"}
                            </span>
                            <span className="font-mono wrap-anywhere">
                              Run：{issue.run_id ?? "—"}
                            </span>
                          </div>
                        </TableCell>
                        <TableCell className="whitespace-normal">
                          <div className="grid gap-1 text-xs">
                            <span>交易日：{issue.trade_date ?? "—"}</span>
                            <span>
                              批次資料日：{issue.batch_data_date ?? "—"}
                            </span>
                            <span>
                              Schema：{issue.schema_id ?? "—"}
                              {issue.schema_version
                                ? `.v${issue.schema_version}`
                                : ""}
                            </span>
                            <span>
                              Raw：{issue.raw_available ? "可取得" : "不可用"}
                            </span>
                            <span className="font-mono wrap-anywhere">
                              Raw payload ID：
                              {boundedPolicyValue(issue.raw_payload_id, 64)}
                            </span>
                            <span>Fetched：{formatDate(issue.fetched_at)}</span>
                            <span className="font-mono wrap-anywhere">
                              Request：{issue.request_key ?? "—"}
                            </span>
                          </div>
                        </TableCell>
                        <TableCell className="whitespace-normal">
                          <DQPolicyDetails detail={issue.policy_detail} />
                        </TableCell>
                        <TableCell className="whitespace-normal">
                          {issue.description ?? "—"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                {issues && issues.pagination.total_pages > 1 && (
                  <nav
                    className="mt-4 flex flex-wrap items-center justify-center gap-1"
                    aria-label="資料品質分頁"
                  >
                    <Button
                      variant="secondary"
                      size="sm"
                      type="button"
                      onClick={() => changePage(issues.pagination.page - 1)}
                      disabled={pending || issues.pagination.page <= 1}
                    >
                      上一頁
                    </Button>
                    {getVisiblePages(
                      issues.pagination.page,
                      issues.pagination.total_pages
                    ).map((page, index, pages) => (
                      <span className="contents" key={page}>
                        {index > 0 && page - (pages[index - 1] ?? page) > 1 && (
                          <span className="px-1 text-muted" aria-hidden="true">
                            …
                          </span>
                        )}
                        <Button
                          variant={
                            page === issues.pagination.page
                              ? "default"
                              : "secondary"
                          }
                          size="sm"
                          type="button"
                          aria-label={`第 ${page} 頁`}
                          aria-current={
                            page === issues.pagination.page ? "page" : undefined
                          }
                          onClick={() => changePage(page)}
                          disabled={pending}
                        >
                          {page}
                        </Button>
                      </span>
                    ))}
                    <Button
                      variant="secondary"
                      size="sm"
                      type="button"
                      onClick={() => changePage(issues.pagination.page + 1)}
                      disabled={
                        pending ||
                        issues.pagination.page >= issues.pagination.total_pages
                      }
                    >
                      下一頁
                    </Button>
                  </nav>
                )}
              </>
            )}
          </>
        )}
      </Panel>
    </>
  )
}

export function CorrectionsPage() {
  const { data, initialLoading } = useOperations()
  const corrections = data?.view === "corrections" ? data : null
  return (
    <>
      <PageIntro
        eyebrow="Audit trail"
        title="修正稽核"
        description="檢視 canonical 資料近期的人工修正紀錄。"
      />
      <Panel
        eyebrow="Recent corrections"
        title="近期修正"
        icon={<Archive size={19} />}
        result={corrections?.corrections ?? null}
        loading={initialLoading}
      >
        {corrections?.corrections.ok &&
          (corrections.corrections.data.data.length === 0 ? (
            <EmptyState>目前沒有修正紀錄</EmptyState>
          ) : (
            <>
              <p className="mt-0 mb-2 text-xs text-muted">
                共 {corrections.corrections.data.pagination.total_records}{" "}
                筆，顯示前 {corrections.corrections.data.data.length} 筆
              </p>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>時間</TableHead>
                    <TableHead>資料表</TableHead>
                    <TableHead>修正者</TableHead>
                    <TableHead>原因</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {corrections.corrections.data.data.map(correction => (
                    <TableRow key={correction.id}>
                      <TableCell>{formatDate(correction.created_at)}</TableCell>
                      <TableCell className="font-mono wrap-anywhere">
                        {correction.table_name}
                      </TableCell>
                      <TableCell>{correction.corrected_by}</TableCell>
                      <TableCell className="whitespace-normal">
                        {correction.correction_reason}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </>
          ))}
      </Panel>
    </>
  )
}

export function RawPayloadsPage() {
  const { data, filters, initialLoading, pending, refresh, setFilters } =
    useOperations()
  const loadDetail = useServerFn(loadRawPayloadDetail)
  const navigate = useNavigate()
  const rawView = data?.view === "rawPayloads" ? data : null
  const rawPayloads = rawView?.rawPayloads.ok ? rawView.rawPayloads.data : null
  const [pageSizeDraft, setPageSizeDraft] = useState(filters.pageSize)
  const [expandedPayloadKeys, setExpandedPayloadKeys] = useState<Set<string>>(
    () => new Set()
  )
  const [payloadDetails, setPayloadDetails] = useState<
    Record<
      string,
      | { status: "loading" }
      | { status: "success"; data: RawPayload }
      | { status: "error"; error: string }
    >
  >({})
  const detailGeneration = useRef(0)

  function resetPayloadDetails() {
    detailGeneration.current += 1
    setExpandedPayloadKeys(new Set())
    setPayloadDetails({})
  }

  function submitAudit(event: FormEvent) {
    event.preventDefault()
    const nextFilters = { ...filters, page: 1, pageSize: pageSizeDraft }
    setFilters(nextFilters)
    resetPayloadDetails()
    void refresh(nextFilters)
  }

  function changePage(page: number) {
    const nextFilters = { ...filters, page }
    setFilters(nextFilters)
    resetPayloadDetails()
    void refresh(nextFilters)
  }

  async function togglePayload(payloadKey: string) {
    if (expandedPayloadKeys.has(payloadKey)) {
      setExpandedPayloadKeys(current => {
        const next = new Set(current)
        next.delete(payloadKey)
        return next
      })
      return
    }
    setExpandedPayloadKeys(current => {
      const next = new Set(current)
      next.add(payloadKey)
      return next
    })
    if (payloadDetails[payloadKey]) return
    const generation = detailGeneration.current
    setPayloadDetails(current => ({
      ...current,
      [payloadKey]: { status: "loading" },
    }))
    try {
      const detail = await loadDetail({ data: { rawPayloadId: payloadKey } })
      if (detailGeneration.current !== generation) return
      setPayloadDetails(current => ({
        ...current,
        [payloadKey]: { status: "success", data: detail },
      }))
    } catch (reason) {
      if (detailGeneration.current !== generation) return
      if (isDashboardAuthenticationError(reason)) {
        await navigate({ to: "/login", replace: true })
        return
      }
      const message =
        reason instanceof Error && reason.message.includes("(404)")
          ? "原始資料已不存在"
          : reason instanceof Error
            ? reason.message
            : "無法取得原始資料"
      setPayloadDetails(current => ({
        ...current,
        [payloadKey]: { status: "error", error: message },
      }))
    }
  }

  return (
    <>
      <PageIntro
        eyebrow="Raw payload retrieval"
        title="原始資料稽核"
        description="依資料集、run 與日期範圍查詢保留中的 raw payload。"
      />
      <Card className="gap-0 p-5">
        <CardHeader className="mb-4 flex grid-cols-none flex-row items-center gap-3 px-0">
          <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent">
            <Search size={19} />
          </span>
          <CardTitle asChild className="text-base tracking-tight">
            <h2>原始資料查詢</h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="px-0">
          <form
            className="mb-4 grid grid-cols-2 items-end gap-2.5 xl:grid-cols-7"
            onSubmit={submitAudit}
          >
            <div className="grid gap-1.5 xl:col-span-2">
              <Label htmlFor="dataset-key">Dataset key</Label>
              <Input
                id="dataset-key"
                value={filters.datasetKey}
                onChange={event =>
                  setFilters({ ...filters, datasetKey: event.target.value })
                }
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="run-id">Run ID</Label>
              <Input
                id="run-id"
                value={filters.runId}
                onChange={event =>
                  setFilters({ ...filters, runId: event.target.value })
                }
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="date-from">起始日期</Label>
              <Input
                id="date-from"
                type="date"
                value={filters.dateFrom}
                onChange={event =>
                  setFilters({ ...filters, dateFrom: event.target.value })
                }
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="date-to">結束日期</Label>
              <Input
                id="date-to"
                type="date"
                value={filters.dateTo}
                onChange={event =>
                  setFilters({ ...filters, dateTo: event.target.value })
                }
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="page-size">每頁筆數</Label>
              <select
                id="page-size"
                className="h-9 w-full rounded-lg border border-line bg-surface px-3 text-sm text-ink outline-none focus-visible:ring-3 focus-visible:ring-accent/20"
                value={pageSizeDraft}
                onChange={event => setPageSizeDraft(Number(event.target.value))}
                disabled={pending}
              >
                <option value={25}>25</option>
                <option value={50}>50</option>
                <option value={100}>100</option>
              </select>
            </div>
            <Button
              className="col-span-2 xl:col-span-1"
              type="submit"
              disabled={pending}
            >
              <Search size={16} /> 查詢
            </Button>
          </form>
          {initialLoading ? (
            <LoadingState label="正在載入原始資料" />
          ) : !data ? (
            <Alert variant="destructive">
              <TriangleAlert />
              <AlertDescription>暫時無法查詢 raw payload</AlertDescription>
            </Alert>
          ) : !rawPayloads ? (
            <Alert variant="destructive">
              <TriangleAlert />
              <AlertDescription>
                {rawView && !rawView.rawPayloads.ok
                  ? rawView.rawPayloads.error
                  : "暫時無法查詢 raw payload"}
              </AlertDescription>
            </Alert>
          ) : rawPayloads.data.length === 0 ? (
            <EmptyState>查無符合條件的原始資料</EmptyState>
          ) : (
            <>
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
                <p className="m-0">
                  第 {rawPayloads.pagination.page} /{" "}
                  {rawPayloads.pagination.total_pages} 頁，共{" "}
                  {rawPayloads.pagination.total_records} 筆，本頁{" "}
                  {rawPayloads.data.length} 筆
                </p>
                {pending && (
                  <span role="status" aria-live="polite">
                    正在更新…
                  </span>
                )}
              </div>
              <Table scrollMode="page">
                <TableHeader>
                  <TableRow>
                    <TableHead>建立時間</TableHead>
                    <TableHead>Dataset</TableHead>
                    <TableHead>來源</TableHead>
                    <TableHead>Run ID</TableHead>
                    <TableHead>保留期限</TableHead>
                    <TableHead>Payload</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rawPayloads.data.map(payload => {
                    const payloadKey = payload.raw_payload_id
                    const expanded = expandedPayloadKeys.has(payloadKey)
                    const detail = payloadDetails[payloadKey]
                    return (
                      <Fragment key={payloadKey}>
                        <TableRow>
                          <TableCell>
                            {formatDate(payload.created_at)}
                          </TableCell>
                          <TableCell className="font-mono wrap-anywhere">
                            {payload.dataset_key}
                          </TableCell>
                          <TableCell>{payload.source}</TableCell>
                          <TableCell className="font-mono wrap-anywhere">
                            {payload.run_id}
                          </TableCell>
                          <TableCell>{formatDate(payload.expire_at)}</TableCell>
                          <TableCell>
                            <Button
                              variant="ghost"
                              size="sm"
                              type="button"
                              aria-expanded={expanded}
                              onClick={() => void togglePayload(payloadKey)}
                            >
                              {expanded
                                ? "收合 JSON"
                                : detail?.status === "loading"
                                  ? "載入中…"
                                  : "檢視 JSON"}
                            </Button>
                          </TableCell>
                        </TableRow>
                        {expanded && (
                          <TableRow>
                            <TableCell
                              colSpan={6}
                              className="bg-surface-soft p-3"
                            >
                              {detail?.status === "loading" ? (
                                <span role="status">正在載入完整 JSON…</span>
                              ) : detail?.status === "error" ? (
                                <Alert variant="destructive">
                                  <TriangleAlert />
                                  <AlertDescription>
                                    {detail.error}
                                  </AlertDescription>
                                </Alert>
                              ) : detail?.status === "success" ? (
                                <pre className="m-0 w-full rounded-lg border border-line bg-surface p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap text-ink wrap-anywhere">
                                  {JSON.stringify(detail.data.payload, null, 2)}
                                </pre>
                              ) : null}
                            </TableCell>
                          </TableRow>
                        )}
                      </Fragment>
                    )
                  })}
                </TableBody>
              </Table>
              {rawPayloads.pagination.total_pages > 1 && (
                <nav
                  className="mt-4 flex flex-wrap items-center justify-center gap-1"
                  aria-label="原始資料分頁"
                >
                  <Button
                    variant="secondary"
                    size="sm"
                    type="button"
                    onClick={() => changePage(rawPayloads.pagination.page - 1)}
                    disabled={pending || rawPayloads.pagination.page <= 1}
                  >
                    上一頁
                  </Button>
                  {getVisiblePages(
                    rawPayloads.pagination.page,
                    rawPayloads.pagination.total_pages
                  ).map((page, index, pages) => (
                    <span className="contents" key={page}>
                      {index > 0 && page - (pages[index - 1] ?? page) > 1 && (
                        <span className="px-1 text-muted" aria-hidden="true">
                          …
                        </span>
                      )}
                      <Button
                        variant={
                          page === rawPayloads.pagination.page
                            ? "default"
                            : "secondary"
                        }
                        size="sm"
                        type="button"
                        aria-label={`第 ${page} 頁`}
                        aria-current={
                          page === rawPayloads.pagination.page
                            ? "page"
                            : undefined
                        }
                        onClick={() => changePage(page)}
                        disabled={pending}
                      >
                        {page}
                      </Button>
                    </span>
                  ))}
                  <Button
                    variant="secondary"
                    size="sm"
                    type="button"
                    onClick={() => changePage(rawPayloads.pagination.page + 1)}
                    disabled={
                      pending ||
                      rawPayloads.pagination.page >=
                        rawPayloads.pagination.total_pages
                    }
                  >
                    下一頁
                  </Button>
                </nav>
              )}
            </>
          )}
        </CardContent>
      </Card>
      <Alert className="mt-5" variant="subtle" role="note">
        <AlertTitle>資料保留</AlertTitle>
        <AlertDescription>
          Raw payload 受保留政策影響，過期資料可能無法從此查詢取得。
        </AlertDescription>
      </Alert>
    </>
  )
}
