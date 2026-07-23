import { useNavigate } from "@tanstack/react-router"
import { useServerFn } from "@tanstack/react-start"
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Clock3,
  Database,
  LogOut,
  RefreshCw,
  Search,
  ShieldCheck,
  TriangleAlert,
  Wifi,
} from "lucide-react"
import { type FormEvent, useCallback, useEffect, useState } from "react"

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
import type { DashboardResponse, PanelResult } from "../../lib/admin-api"
import { loadDashboard } from "../../lib/admin.functions"
import { logout } from "../../lib/auth.functions"

const EMPTY_FILTERS = {
  datasetKey: "",
  runId: "",
  dateFrom: "",
  dateTo: "",
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

function Metric({
  label,
  value,
  detail,
  wide = false,
}: {
  label: string
  value: React.ReactNode
  detail?: React.ReactNode
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
  icon: React.ReactNode
  result: PanelResult<unknown> | null
  loading?: boolean
  children: React.ReactNode
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

function EmptyState({ children }: { children: React.ReactNode }) {
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
      <span className="size-2 shrink-0 animate-pulse rounded-full bg-muted" />
    )
  }
  if (state === "healthy") {
    return (
      <span className="size-2 shrink-0 rounded-full bg-accent ring-4 ring-accent/15" />
    )
  }
  if (state === "degraded") {
    return (
      <span className="size-2 shrink-0 rounded-full bg-warning ring-4 ring-warning/15" />
    )
  }
  return (
    <span className="size-2 shrink-0 rounded-full bg-danger ring-4 ring-danger/15" />
  )
}

export default function OperationsConsole({ username }: { username: string }) {
  const load = useServerFn(loadDashboard)
  const logoutFn = useServerFn(logout)
  const navigate = useNavigate()
  const [data, setData] = useState<DashboardResponse | null>(null)
  const [error, setError] = useState("")
  const [pending, setPending] = useState(true)
  const [filters, setFilters] = useState(EMPTY_FILTERS)

  const refresh = useCallback(
    async (nextFilters: typeof EMPTY_FILTERS) => {
      setPending(true)
      setError("")
      try {
        const result = await load({
          data: { audit: nextFilters },
        })
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

  function submitAudit(event: FormEvent) {
    event.preventDefault()
    void refresh(filters)
  }

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
    <main className="mx-auto w-full max-w-screen-2xl px-3 py-6 sm:px-5 sm:py-11 sm:pb-16">
      <section className="grid grid-cols-1 items-end gap-8 px-1 pt-3 pb-8 lg:grid-cols-2 lg:gap-12">
        <div>
          <p className="mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase">
            FinDB Operations / Read only
          </p>
          <h1 className="my-2.5 text-4xl leading-none tracking-tighter sm:text-6xl lg:text-7xl">
            資料導入營運台
          </h1>
          <p className="m-0 max-w-2xl text-base text-muted">
            集中檢查導入穩定度、資料完整性與修正稽核，所有操作皆為唯讀。
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
              {pending ? (
                <RefreshCw className="animate-spin" size={17} />
              ) : (
                <Wifi size={17} />
              )}
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
        {data && (
          <Button
            className="ml-auto"
            variant="ghost"
            size="sm"
            type="button"
            onClick={() => void refresh(filters)}
            disabled={pending}
          >
            <RefreshCw className={pending ? "animate-spin" : ""} size={15} />
            手動更新
          </Button>
        )}
      </Alert>
      {connectionState === "failed" && (
        <Alert className="mb-3" variant="destructive">
          <AlertTriangle size={18} />
          <AlertTitle>Admin API 連線失敗</AlertTitle>
          <AlertDescription>
            所有 Admin API 查詢均失敗，請確認後端服務與 Dashboard server 設定。
          </AlertDescription>
        </Alert>
      )}
      {connectionState === "degraded" && (
        <Alert className="mb-3" variant="warning" role="status">
          <AlertTriangle size={18} />
          <AlertTitle>部分服務異常</AlertTitle>
          <AlertDescription>
            部分資料來源暫時無法取得；其餘成功面板仍為有效結果。
          </AlertDescription>
        </Alert>
      )}

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Panel
          eyebrow="Ingestion stability"
          title="佇列與 Worker"
          icon={<Database size={19} />}
          result={data?.queue ?? null}
          loading={initialLoading}
        >
          {data?.queue.ok && (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Metric
                label="排隊中"
                value={data.queue.data.counts.queued ?? 0}
              />
              <Metric
                label="處理中"
                value={data.queue.data.counts.processing ?? 0}
              />
              <Metric
                label="重試耗盡"
                value={data.queue.data.retry_exhausted}
              />
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

        <Panel
          eyebrow="Completeness"
          title="缺漏交付"
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

        <Panel
          eyebrow="Correctness"
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

        <Panel
          eyebrow="Audit trail"
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
                        <TableCell>
                          {formatDate(correction.created_at)}
                        </TableCell>
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
      </div>

      <Card className="mt-5 gap-0 p-5">
        <CardHeader className="mb-4 flex grid-cols-none flex-row items-center gap-3 px-0">
          <span className="inline-flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent">
            <Search size={19} />
          </span>
          <div>
            <p className="mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase">
              Raw payload retrieval
            </p>
            <CardTitle asChild className="text-base tracking-tight">
              <h2>原始資料稽核查詢</h2>
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent className="px-0">
          <form
            className="mb-4 grid grid-cols-2 items-end gap-2.5 lg:grid-cols-6"
            onSubmit={submitAudit}
          >
            <div className="grid gap-1.5 lg:col-span-2">
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
            <Button
              className="col-span-2 lg:col-span-1"
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
          ) : !data.rawPayloads.ok ? (
            <Alert variant="destructive">
              <TriangleAlert />
              <AlertDescription>{data.rawPayloads.error}</AlertDescription>
            </Alert>
          ) : data.rawPayloads.data.data.length === 0 ? (
            <EmptyState>查無符合條件的原始資料</EmptyState>
          ) : (
            <>
              <p className="mt-0 mb-2 text-xs text-muted">
                共 {data.rawPayloads.data.pagination.total_records} 筆，顯示前{" "}
                {data.rawPayloads.data.data.length} 筆
              </p>
              <Table>
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
                  {data.rawPayloads.data.data.map(payload => (
                    <TableRow
                      key={`${payload.run_id}:${payload.idempotency_key}`}
                    >
                      <TableCell>{formatDate(payload.created_at)}</TableCell>
                      <TableCell className="font-mono wrap-anywhere">
                        {payload.dataset_key}
                      </TableCell>
                      <TableCell>{payload.source}</TableCell>
                      <TableCell className="font-mono wrap-anywhere">
                        {payload.run_id}
                      </TableCell>
                      <TableCell>{formatDate(payload.expire_at)}</TableCell>
                      <TableCell>
                        <details className="min-w-24">
                          <summary className="cursor-pointer font-extrabold whitespace-nowrap text-accent">
                            檢視 JSON
                          </summary>
                          <pre className="mt-2 max-h-80 w-96 overflow-auto rounded-lg border border-line bg-surface-soft p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-ink wrap-anywhere sm:w-xl lg:w-2xl">
                            {JSON.stringify(payload.payload, null, 2)}
                          </pre>
                        </details>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </>
          )}
        </CardContent>
      </Card>

      <Alert className="mt-5" variant="subtle" role="note">
        <AlertTitle>目前限制</AlertTitle>
        <AlertDescription>
          <ul className="list-disc space-y-1 pl-4">
            <li>後端尚無歷史 run 趨勢 API，因此本頁只呈現即時佇列狀態。</li>
            <li>Raw payload 受保留政策影響，過期資料可能無法從此查詢取得。</li>
          </ul>
        </AlertDescription>
      </Alert>
    </main>
  )
}
