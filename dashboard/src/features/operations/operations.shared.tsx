import { useQueryClient } from "@tanstack/react-query"
import { Link, Outlet, useNavigate } from "@tanstack/react-router"
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
  RefreshCw,
  Search,
  ShieldCheck,
  TriangleAlert,
  Users,
} from "lucide-react"
import type { ReactNode } from "react"

import { ProtectedQueryScopeProvider } from "../../components/ProtectedQueryScope"
import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../../components/ui/card"
import { Skeleton } from "../../components/ui/skeleton"
import type {
  FreshnessStatus,
  MarketFreshness,
  PanelResult,
} from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import { canViewUsers } from "../../lib/admin-permissions"
import { logout } from "../../lib/auth.functions"
import {
  operationsKeys,
  useOperationsDashboardRefresh,
  useOperationsIsFetching,
  type OperationsDashboardQuery,
} from "./operations.queries"
import { type OperationsDashboardState } from "./operations.queries"

export type { OperationsDashboardQuery } from "./operations.queries"

export function formatDate(value: string | null | undefined) {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-TW", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Taipei",
  }).format(new Date(value))
}

export function formatRelativeDate(value: string | null | undefined) {
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
  return new Intl.RelativeTimeFormat("zh-TW", { numeric: "auto" }).format(
    Math.round(amount),
    unit
  )
}

export function DateWithRelative({
  value,
}: {
  value: string | null | undefined
}) {
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

export function formatAge(seconds: number | null) {
  if (seconds === null) return "無資料"
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分鐘`
  return `${Math.round(seconds / 3600)} 小時`
}

export function formatScheduledTime(value: string) {
  const [hour, minute] = value.split(":")
  if (!hour || !minute) return value
  return `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`
}

export function formatSchedulerState(value: string) {
  if (value === "running") return "執行中"
  if (value === "stopped") return "已停止"
  if (value === "idle") return "閒置"
  if (value === "unknown") return "未知"
  return value
}

export function schedulerStateVariant(value: string) {
  if (value === "running") return "default" as const
  if (value === "stopped" || value === "idle") return "secondary" as const
  return "warning" as const
}

export function schedulerIsStale(scheduler: {
  heartbeat_age_seconds: number | null
}) {
  return (
    scheduler.heartbeat_age_seconds === null ||
    scheduler.heartbeat_age_seconds > 90
  )
}

export function getVisiblePages(currentPage: number, totalPages: number) {
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

export function Metric({
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

export function PageIntro({
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

export function LoadingState({ label = "正在載入資料" }: { label?: string }) {
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

export function RefreshStatus({
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

export function EmptyState({ children }: { children: ReactNode }) {
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

export function Panel({
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

export function OperationsDashboardStatus({
  state,
}: {
  state: OperationsDashboardState
}) {
  return (
    <>
      {state.fatalError && (
        <Alert className="mb-3" variant="destructive">
          <AlertTriangle size={18} />
          <AlertTitle>無法更新營運資料</AlertTitle>
          <AlertDescription>{state.fatalError}</AlertDescription>
        </Alert>
      )}
      <Alert
        className="mb-5 flex items-center gap-2.5 text-xs text-muted"
        role={state.initialLoading ? "status" : undefined}
        aria-live="polite"
      >
        <ConnectionIndicator state={state.connectionState} />
        <strong className="text-ink">{state.connectionLabel}</strong>
        <span>
          {state.response
            ? `${state.successfulPanels}/${state.panelResults.length} 個資料來源成功 · 最後更新 ${formatDate(state.response.fetchedAt)}`
            : "正在載入營運資料"}
        </span>
      </Alert>
      {state.connectionState === "failed" && !state.initialLoading && (
        <Alert className="mb-5" variant="destructive">
          <AlertTriangle size={18} />
          <AlertTitle>Admin API 連線失敗</AlertTitle>
          <AlertDescription>
            所有 Admin API 查詢均失敗，請確認後端服務與 Dashboard server 設定。
          </AlertDescription>
        </Alert>
      )}
      {state.connectionState === "degraded" && (
        <Alert className="mb-5" variant="warning" role="status">
          <AlertTriangle size={18} />
          <AlertTitle>部分服務異常</AlertTitle>
          <AlertDescription>
            部分資料來源暫時無法取得；其餘成功頁面仍為有效結果。
          </AlertDescription>
        </Alert>
      )}
    </>
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
  const logoutFn = useServerFn(logout)
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const pending = useOperationsIsFetching()
  const refresh = useOperationsDashboardRefresh()

  async function signOut() {
    await logoutFn()
    queryClient.clear()
    await navigate({ to: "/login" })
  }

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
            <Button
              type="button"
              onClick={() => void refresh()}
              disabled={pending}
            >
              <RefreshCw className={pending ? "animate-spin" : ""} size={17} />
              重新整理
            </Button>
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

      <div className="grid gap-5 lg:grid-cols-[15rem_minmax(0,1fr)] lg:items-start">
        <aside className="min-w-0 lg:sticky lg:top-21">
          <nav
            className="flex gap-1 overflow-x-auto rounded-xl border border-line bg-surface p-2 shadow-sm lg:flex-col lg:overflow-visible"
            aria-label="營運資料分類"
          >
            {navigation
              .filter(
                item =>
                  !("ownerOnly" in item) ||
                  !item.ownerOnly ||
                  canViewUsers(role)
              )
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
        <div className="min-w-0">
          <ProtectedQueryScopeProvider value={`${username}:${role}`}>
            <Outlet />
          </ProtectedQueryScopeProvider>
        </div>
      </div>
    </main>
  )
}

export const SLOT_SEMANTIC_LABELS: Record<string, string> = {
  western_markets_window: "西方市場窗口",
  global_markets_window: "全球市場窗口",
  taiwan_market_window: "台灣市場窗口",
  asia_pacific_markets_window: "亞太市場窗口",
}

export const STATUS_LABELS: Record<FreshnessStatus, string> = {
  not_due: "尚未到期",
  fresh: "已更新",
  partial: "部分完成",
  late: "延遲",
  failed: "失敗",
  never_received: "從未收到",
}

export function freshnessVariant(status: FreshnessStatus) {
  if (status === "fresh") return "default" as const
  if (status === "not_due" || status === "partial") return "warning" as const
  return "destructive" as const
}

export function FeedDetails({ feeds }: { feeds: MarketFreshness["feeds"] }) {
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

export function panelErrorAt(query: OperationsDashboardQuery, index: number) {
  return query.data?.refreshErrors[index] ?? ""
}

export function panelResultWithError<T>(
  result: PanelResult<T> | null,
  refreshError: string
) {
  if (!result || !refreshError || !result.ok) return result
  return result
}

export function operationPanelIcon(name: "alert" | "database") {
  return name === "database" ? (
    <Database size={19} />
  ) : (
    <TriangleAlert size={19} />
  )
}

export { operationsKeys }
