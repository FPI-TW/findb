import { Link, Outlet, useNavigate } from "@tanstack/react-router"
import { useServerFn } from "@tanstack/react-start"
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Clock3,
  Database,
  Gauge,
  LogOut,
  RefreshCw,
  Search,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react"
import {
  createContext,
  Fragment,
  type FormEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react"

import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
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
import type {
  DashboardRequest,
  DashboardResponse,
  PanelResult,
} from "../../lib/admin-api"
import { loadDashboard } from "../../lib/admin.functions"
import { logout } from "../../lib/auth.functions"

const EMPTY_FILTERS: DashboardRequest["audit"] = {
  datasetKey: "",
  runId: "",
  dateFrom: "",
  dateTo: "",
  page: 1,
  pageSize: 25,
}

type OperationsContextValue = {
  data: DashboardResponse | null
  error: string
  pending: boolean
  initialLoading: boolean
  filters: DashboardRequest["audit"]
  setFilters: (filters: DashboardRequest["audit"]) => void
  refresh: (filters: DashboardRequest["audit"]) => Promise<void>
}

const OperationsContext = createContext<OperationsContextValue | null>(null)

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
  }).format(new Date(value))
}

function formatAge(seconds: number | null) {
  if (seconds === null) return "無資料"
  if (seconds < 60) return `${Math.round(seconds)} 秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分鐘`
  return `${Math.round(seconds / 3600)} 小時`
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
] as const

export default function OperationsLayout({ username }: { username: string }) {
  const load = useServerFn(loadDashboard)
  const logoutFn = useServerFn(logout)
  const navigate = useNavigate()
  const [data, setData] = useState<DashboardResponse | null>(null)
  const [error, setError] = useState("")
  const [pending, setPending] = useState(true)
  const [filters, setFilters] = useState(EMPTY_FILTERS)

  const refresh = useCallback(
    async (nextFilters: DashboardRequest["audit"]) => {
      setPending(true)
      setError("")
      try {
        const result = await load({ data: { audit: nextFilters } })
        setData(result)
      } catch (reason) {
        setError(
          reason instanceof Error ? reason.message : "無法連線至 FinDB API。"
        )
      } finally {
        setPending(false)
      }
    },
    [load]
  )

  useEffect(() => {
    void refresh(EMPTY_FILTERS)
  }, [refresh])

  async function signOut() {
    await logoutFn()
    await navigate({ to: "/login" })
  }

  const initialLoading = data === null && pending && error === ""
  const panelResults = data
    ? [
        data.queue,
        data.deliveries,
        data.issues,
        data.corrections,
        data.rawPayloads,
      ]
    : []
  const successfulPanels = panelResults.filter(result => result.ok).length
  const connectionState =
    data === null
      ? pending
        ? "loading"
        : "failed"
      : successfulPanels === panelResults.length
        ? "healthy"
        : successfulPanels === 0
          ? "failed"
          : "degraded"
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
            FinDB Operations / Read only
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
            <small className="text-xs text-muted">
              Admin key 僅由 Dashboard server 讀取
            </small>
          </div>
          <div className="flex flex-col items-stretch gap-2 sm:flex-row">
            <Button
              type="button"
              onClick={() => void refresh(filters)}
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

      {error && (
        <Alert className="mb-3" variant="destructive">
          <AlertTriangle size={18} />
          <AlertTitle>無法更新營運資料</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

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

      {connectionState === "failed" && (
        <Alert className="mb-5" variant="destructive">
          <AlertTriangle size={18} />
          <AlertTitle>Admin API 連線失敗</AlertTitle>
          <AlertDescription>
            所有 Admin API 查詢均失敗，請確認後端服務與 Dashboard server 設定。
          </AlertDescription>
        </Alert>
      )}
      {connectionState === "degraded" && (
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
            {navigation.map(item => {
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
            pending,
            initialLoading,
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

export function OperationsOverviewPage() {
  const { data, initialLoading } = useOperations()
  return (
    <>
      <PageIntro
        eyebrow="Ingestion health"
        title="導入概況"
        description="檢查佇列、Worker heartbeat 與 outbox 的即時健康狀態。"
      />
      <Panel
        eyebrow="Queue and worker"
        title="佇列與 Worker"
        icon={<Database size={19} />}
        result={data?.queue ?? null}
        loading={initialLoading}
      >
        {data?.queue.ok && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Metric label="排隊中" value={data.queue.data.counts.queued ?? 0} />
            <Metric
              label="處理中"
              value={data.queue.data.counts.processing ?? 0}
            />
            <Metric label="重試耗盡" value={data.queue.data.retry_exhausted} />
            <Metric label="過期租約" value={data.queue.data.expired_leases} />
            <Metric
              wide
              label="Worker heartbeat"
              value={formatAge(data.queue.data.worker_heartbeat_age_seconds)}
              detail={formatDate(data.queue.data.last_worker_heartbeat_at)}
            />
            <Metric
              wide
              label="未發布 outbox"
              value={data.queue.data.unpublished_outbox}
            />
          </div>
        )}
      </Panel>
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
        result={data?.deliveries ?? null}
        loading={initialLoading}
      >
        {data?.deliveries.ok &&
          (data.deliveries.data.data.length === 0 ? (
            <EmptyState>目前沒有未解決的交付缺漏</EmptyState>
          ) : (
            <>
              <p className="mt-0 mb-2 text-xs text-muted">
                共 {data.deliveries.data.pagination.total_records} 筆，顯示前{" "}
                {data.deliveries.data.data.length} 筆
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
                  {data.deliveries.data.data.map(alert => (
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

export function QualityPage() {
  const { data, initialLoading } = useOperations()
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
        result={data?.issues ?? null}
        loading={initialLoading}
      >
        {data?.issues.ok &&
          (data.issues.data.data.length === 0 ? (
            <EmptyState>目前沒有未解決的資料品質問題</EmptyState>
          ) : (
            <>
              <p className="mt-0 mb-2 text-xs text-muted">
                共 {data.issues.data.pagination.total_records} 筆，顯示前{" "}
                {data.issues.data.data.length} 筆
              </p>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>嚴重度</TableHead>
                    <TableHead>類型</TableHead>
                    <TableHead>交易日</TableHead>
                    <TableHead>說明</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.issues.data.data.map(issue => (
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
                      <TableCell>{issue.trade_date ?? "—"}</TableCell>
                      <TableCell className="whitespace-normal">
                        {issue.description ?? "—"}
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

export function CorrectionsPage() {
  const { data, initialLoading } = useOperations()
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
        result={data?.corrections ?? null}
        loading={initialLoading}
      >
        {data?.corrections.ok &&
          (data.corrections.data.data.length === 0 ? (
            <EmptyState>目前沒有修正紀錄</EmptyState>
          ) : (
            <>
              <p className="mt-0 mb-2 text-xs text-muted">
                共 {data.corrections.data.pagination.total_records} 筆，顯示前{" "}
                {data.corrections.data.data.length} 筆
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
                  {data.corrections.data.data.map(correction => (
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
  const rawPayloads = data?.rawPayloads.ok ? data.rawPayloads.data : null
  const [pageSizeDraft, setPageSizeDraft] = useState(filters.pageSize)
  const [expandedPayloadKeys, setExpandedPayloadKeys] = useState<Set<string>>(
    () => new Set()
  )

  function submitAudit(event: FormEvent) {
    event.preventDefault()
    const nextFilters = { ...filters, page: 1, pageSize: pageSizeDraft }
    setFilters(nextFilters)
    setExpandedPayloadKeys(new Set())
    void refresh(nextFilters)
  }

  function changePage(page: number) {
    const nextFilters = { ...filters, page }
    setFilters(nextFilters)
    setExpandedPayloadKeys(new Set())
    void refresh(nextFilters)
  }

  function togglePayload(payloadKey: string) {
    setExpandedPayloadKeys(current => {
      const next = new Set(current)
      if (next.has(payloadKey)) {
        next.delete(payloadKey)
      } else {
        next.add(payloadKey)
      }
      return next
    })
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
                {data.rawPayloads.ok
                  ? "暫時無法查詢 raw payload"
                  : data.rawPayloads.error}
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
              <Table scrollMode="page" className="[&_th]:top-16">
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
                    const payloadKey = `${payload.run_id}:${payload.idempotency_key}`
                    const expanded = expandedPayloadKeys.has(payloadKey)
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
                              onClick={() => togglePayload(payloadKey)}
                            >
                              {expanded ? "收合 JSON" : "檢視 JSON"}
                            </Button>
                          </TableCell>
                        </TableRow>
                        {expanded && (
                          <TableRow>
                            <TableCell
                              colSpan={6}
                              className="bg-surface-soft p-3"
                            >
                              <pre className="m-0 w-full rounded-lg border border-line bg-surface p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap text-ink wrap-anywhere">
                                {JSON.stringify(payload.payload, null, 2)}
                              </pre>
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
