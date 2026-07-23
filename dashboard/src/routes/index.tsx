import { createFileRoute, redirect } from "@tanstack/react-router"
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

import type { DashboardResponse, PanelResult } from "../lib/admin-api"
import { loadDashboard } from "../lib/admin.functions"
import { getSession, logout } from "../lib/auth.functions"

const EMPTY_FILTERS = {
  datasetKey: "",
  runId: "",
  dateFrom: "",
  dateTo: "",
}

const BUTTON_CLASS =
  "inline-flex cursor-pointer items-center justify-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-extrabold whitespace-nowrap text-white disabled:cursor-not-allowed disabled:opacity-55"
const SECONDARY_BUTTON_CLASS = `${BUTTON_CLASS} border border-line bg-surface-soft text-ink`
const INPUT_CLASS =
  "w-full min-w-0 rounded-lg border border-line bg-surface px-3 py-2.5 text-ink outline-none focus:border-accent focus:ring-3 focus:ring-accent/15"
const EYEBROW_CLASS =
  "mb-1 font-mono text-xs font-medium tracking-widest text-accent uppercase"
const PANEL_CLASS =
  "min-w-0 rounded-2xl border border-line bg-surface p-5 shadow-panel"
const PANEL_HEADER_CLASS = "mb-4 flex items-center gap-3"
const PANEL_ICON_CLASS =
  "inline-flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent"
const STATE_CLASS =
  "flex min-h-18 items-center justify-center gap-2 rounded-lg p-3.5 text-center text-xs"
const RESULT_COUNT_CLASS = "mt-0 mb-2 text-xs text-muted"
const TABLE_WRAP_CLASS =
  "max-h-80 w-full overflow-auto rounded-lg border border-line"
const TABLE_CLASS = "w-full border-collapse text-xs"
const TABLE_HEADER_CELL_CLASS =
  "sticky top-0 border-b border-line bg-surface-soft px-2.5 py-2 text-left align-top text-xs tracking-wide whitespace-nowrap text-muted uppercase"
const TABLE_CELL_CLASS = "border-b border-line px-2.5 py-2 text-left align-top"
const METRIC_CLASS = "rounded-lg border border-line bg-surface-soft p-3"

export const Route = createFileRoute("/")({
  beforeLoad: async () => {
    const session = await getSession()
    if (!session.authenticated) throw redirect({ to: "/login" })
    return { username: session.username }
  },
  component: OperationsConsole,
})

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
    <div className={`${METRIC_CLASS} ${wide ? "col-span-2" : ""}`}>
      <span className="block text-xs text-muted">{label}</span>
      <strong className="my-1 block font-mono text-xl">{value}</strong>
      {detail && <small className="block text-xs text-muted">{detail}</small>}
    </div>
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
    <section className={PANEL_CLASS}>
      <header className={PANEL_HEADER_CLASS}>
        <span className={PANEL_ICON_CLASS}>{icon}</span>
        <div>
          <p className={EYEBROW_CLASS}>{eyebrow}</p>
          <h2 className="m-0 text-base tracking-tight">{title}</h2>
        </div>
      </header>
      {loading ? (
        <LoadingState />
      ) : result?.ok ? (
        children
      ) : (
        <div className={`${STATE_CLASS} bg-danger-soft text-danger`}>
          <TriangleAlert size={18} />
          <span>{result?.error ?? "暫時無法顯示資料"}</span>
        </div>
      )}
    </section>
  )
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className={`${STATE_CLASS} bg-accent-soft text-accent`}>
      <CheckCircle2 size={18} />
      <span>{children}</span>
    </div>
  )
}

function LoadingState({ label = "正在載入資料" }: { label?: string }) {
  return (
    <div
      className={`${STATE_CLASS} flex-col items-stretch bg-surface-soft`}
      role="status"
      aria-live="polite"
    >
      <span className="sr-only">{label}</span>
      <span className="h-3 w-2/5 animate-pulse rounded-full bg-line" />
      <span className="h-3 w-full animate-pulse rounded-full bg-line" />
      <span className="h-3 w-3/4 animate-pulse rounded-full bg-line" />
    </div>
  )
}

function OperationsConsole() {
  const load = useServerFn(loadDashboard)
  const logoutFn = useServerFn(logout)
  const navigate = Route.useNavigate()
  const { username } = Route.useRouteContext()
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
  const statusDotClass = {
    loading: "bg-muted animate-pulse",
    healthy: "bg-accent ring-4 ring-accent/15",
    degraded: "bg-warning ring-4 ring-warning/15",
    failed: "bg-danger ring-4 ring-danger/15",
  }[connectionState]

  return (
    <main className="mx-auto w-full max-w-screen-2xl px-3 py-6 sm:px-5 sm:py-11 sm:pb-16">
      <section className="grid grid-cols-1 items-end gap-8 px-1 pt-3 pb-8 lg:grid-cols-2 lg:gap-12">
        <div>
          <p className={EYEBROW_CLASS}>FinDB Operations / Read only</p>
          <h1 className="my-2.5 text-4xl leading-none tracking-tighter sm:text-6xl lg:text-7xl">
            資料導入營運台
          </h1>
          <p className="m-0 max-w-2xl text-base text-muted">
            集中檢查導入穩定度、資料完整性與修正稽核，所有操作皆為唯讀。
          </p>
        </div>
        <div className="flex flex-col items-stretch justify-between gap-5 rounded-2xl border border-line bg-surface p-4 shadow-panel sm:flex-row sm:items-center">
          <div className="grid gap-1">
            <span className="text-xs text-muted">已登入</span>
            <strong className="font-mono">{username}</strong>
            <small className="text-xs text-muted">
              Admin key 僅由 Dashboard server 讀取
            </small>
          </div>
          <div className="flex flex-col items-stretch gap-2 sm:flex-row">
            <button
              className={BUTTON_CLASS}
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
            </button>
            <button
              className={SECONDARY_BUTTON_CLASS}
              type="button"
              onClick={() => void signOut()}
            >
              <LogOut size={16} />
              登出
            </button>
          </div>
        </div>
      </section>

      {error && (
        <div
          className="mb-3 flex items-center gap-2.5 rounded-xl border border-danger/30 bg-danger-soft px-3.5 py-3 text-xs text-danger"
          role="alert"
        >
          <AlertTriangle size={18} />
          {error}
        </div>
      )}

      <div
        className="mb-5 flex items-center gap-2.5 rounded-xl border border-line bg-surface px-3.5 py-3 text-xs text-muted"
        role={initialLoading ? "status" : undefined}
        aria-live="polite"
      >
        <span className={`size-2 shrink-0 rounded-full ${statusDotClass}`} />
        <strong className="text-ink">{connectionLabel}</strong>
        <span>
          {data
            ? `${successfulPanels}/${panelResults.length} 個資料來源成功 · 最後更新 ${formatDate(data.fetchedAt)}`
            : "正在載入營運資料"}
        </span>
        {data && (
          <button
            className="ml-auto inline-flex cursor-pointer items-center gap-2 bg-transparent py-1 font-bold text-accent disabled:cursor-not-allowed disabled:opacity-55"
            type="button"
            onClick={() => void refresh(filters)}
            disabled={pending}
          >
            <RefreshCw className={pending ? "animate-spin" : ""} size={15} />
            手動更新
          </button>
        )}
      </div>
      {connectionState === "failed" && (
        <div
          className="mb-3 flex items-center gap-2.5 rounded-xl border border-danger/30 bg-danger-soft px-3.5 py-3 text-xs text-danger"
          role="alert"
        >
          <AlertTriangle size={18} />
          所有 Admin API 查詢均失敗，請確認後端服務與 Dashboard server 設定。
        </div>
      )}
      {connectionState === "degraded" && (
        <div
          className="mb-3 flex items-center gap-2.5 rounded-xl border border-warning/30 bg-warning/10 px-3.5 py-3 text-xs text-warning"
          role="status"
        >
          <AlertTriangle size={18} />
          部分資料來源暫時無法取得；其餘成功面板仍為有效結果。
        </div>
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
                <p className={RESULT_COUNT_CLASS}>
                  共 {data.deliveries.data.pagination.total_records} 筆，顯示前{" "}
                  {data.deliveries.data.data.length} 筆
                </p>
                <div className={TABLE_WRAP_CLASS}>
                  <table className={TABLE_CLASS}>
                    <thead>
                      <tr>
                        <th className={TABLE_HEADER_CELL_CLASS}>資料集</th>
                        <th className={TABLE_HEADER_CELL_CLASS}>來源</th>
                        <th className={TABLE_HEADER_CELL_CLASS}>預期日期</th>
                        <th className={TABLE_HEADER_CELL_CLASS}>首次偵測</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.deliveries.data.data.map(alert => (
                        <tr key={alert.alert_id}>
                          <td
                            className={`${TABLE_CELL_CLASS} font-mono wrap-anywhere`}
                          >
                            {alert.dataset_key}
                          </td>
                          <td className={TABLE_CELL_CLASS}>{alert.source}</td>
                          <td className={TABLE_CELL_CLASS}>
                            {alert.expected_data_date}
                          </td>
                          <td className={TABLE_CELL_CLASS}>
                            {formatDate(alert.first_detected_at)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
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
                <p className={RESULT_COUNT_CLASS}>
                  共 {data.issues.data.pagination.total_records} 筆，顯示前{" "}
                  {data.issues.data.data.length} 筆
                </p>
                <div className={TABLE_WRAP_CLASS}>
                  <table className={TABLE_CLASS}>
                    <thead>
                      <tr>
                        <th className={TABLE_HEADER_CELL_CLASS}>嚴重度</th>
                        <th className={TABLE_HEADER_CELL_CLASS}>類型</th>
                        <th className={TABLE_HEADER_CELL_CLASS}>交易日</th>
                        <th className={TABLE_HEADER_CELL_CLASS}>說明</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.issues.data.data.map(issue => (
                        <tr key={issue.id}>
                          <td className={TABLE_CELL_CLASS}>
                            <span
                              className={`inline-flex rounded-full px-2 py-0.5 font-extrabold ${
                                issue.severity === "error"
                                  ? "bg-danger-soft text-danger"
                                  : "bg-warning/15 text-warning"
                              }`}
                            >
                              {issue.severity}
                            </span>
                          </td>
                          <td
                            className={`${TABLE_CELL_CLASS} font-mono wrap-anywhere`}
                          >
                            {issue.issue_type}
                          </td>
                          <td className={TABLE_CELL_CLASS}>
                            {issue.trade_date ?? "—"}
                          </td>
                          <td className={TABLE_CELL_CLASS}>
                            {issue.description ?? "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
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
                <p className={RESULT_COUNT_CLASS}>
                  共 {data.corrections.data.pagination.total_records} 筆，顯示前{" "}
                  {data.corrections.data.data.length} 筆
                </p>
                <div className={TABLE_WRAP_CLASS}>
                  <table className={TABLE_CLASS}>
                    <thead>
                      <tr>
                        <th className={TABLE_HEADER_CELL_CLASS}>時間</th>
                        <th className={TABLE_HEADER_CELL_CLASS}>資料表</th>
                        <th className={TABLE_HEADER_CELL_CLASS}>修正者</th>
                        <th className={TABLE_HEADER_CELL_CLASS}>原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.corrections.data.data.map(correction => (
                        <tr key={correction.id}>
                          <td className={TABLE_CELL_CLASS}>
                            {formatDate(correction.created_at)}
                          </td>
                          <td
                            className={`${TABLE_CELL_CLASS} font-mono wrap-anywhere`}
                          >
                            {correction.table_name}
                          </td>
                          <td className={TABLE_CELL_CLASS}>
                            {correction.corrected_by}
                          </td>
                          <td className={TABLE_CELL_CLASS}>
                            {correction.correction_reason}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            ))}
        </Panel>
      </div>

      <section className={`${PANEL_CLASS} mt-5`}>
        <header className={PANEL_HEADER_CLASS}>
          <span className={PANEL_ICON_CLASS}>
            <Search size={19} />
          </span>
          <div>
            <p className={EYEBROW_CLASS}>Raw payload retrieval</p>
            <h2 className="m-0 text-base tracking-tight">原始資料稽核查詢</h2>
          </div>
        </header>
        <form
          className="mb-4 grid grid-cols-2 items-end gap-2.5 lg:grid-cols-6"
          onSubmit={submitAudit}
        >
          <label className="grid gap-1.5 text-xs font-bold text-muted lg:col-span-2">
            Dataset key
            <input
              className={INPUT_CLASS}
              value={filters.datasetKey}
              onChange={event =>
                setFilters({ ...filters, datasetKey: event.target.value })
              }
            />
          </label>
          <label className="grid gap-1.5 text-xs font-bold text-muted">
            Run ID
            <input
              className={INPUT_CLASS}
              value={filters.runId}
              onChange={event =>
                setFilters({ ...filters, runId: event.target.value })
              }
            />
          </label>
          <label className="grid gap-1.5 text-xs font-bold text-muted">
            起始日期
            <input
              className={INPUT_CLASS}
              type="date"
              value={filters.dateFrom}
              onChange={event =>
                setFilters({ ...filters, dateFrom: event.target.value })
              }
            />
          </label>
          <label className="grid gap-1.5 text-xs font-bold text-muted">
            結束日期
            <input
              className={INPUT_CLASS}
              type="date"
              value={filters.dateTo}
              onChange={event =>
                setFilters({ ...filters, dateTo: event.target.value })
              }
            />
          </label>
          <button
            className={`${BUTTON_CLASS} col-span-2 lg:col-span-1`}
            type="submit"
            disabled={pending}
          >
            <Search size={16} /> 查詢
          </button>
        </form>
        {initialLoading ? (
          <LoadingState label="正在載入原始資料" />
        ) : !data ? (
          <div className={`${STATE_CLASS} bg-danger-soft text-danger`}>
            暫時無法查詢 raw payload
          </div>
        ) : !data.rawPayloads.ok ? (
          <div className={`${STATE_CLASS} bg-danger-soft text-danger`}>
            {data.rawPayloads.error}
          </div>
        ) : data.rawPayloads.data.data.length === 0 ? (
          <EmptyState>查無符合條件的原始資料</EmptyState>
        ) : (
          <>
            <p className={RESULT_COUNT_CLASS}>
              共 {data.rawPayloads.data.pagination.total_records} 筆，顯示前{" "}
              {data.rawPayloads.data.data.length} 筆
            </p>
            <div className={TABLE_WRAP_CLASS}>
              <table className={TABLE_CLASS}>
                <thead>
                  <tr>
                    <th className={TABLE_HEADER_CELL_CLASS}>建立時間</th>
                    <th className={TABLE_HEADER_CELL_CLASS}>Dataset</th>
                    <th className={TABLE_HEADER_CELL_CLASS}>來源</th>
                    <th className={TABLE_HEADER_CELL_CLASS}>Run ID</th>
                    <th className={TABLE_HEADER_CELL_CLASS}>保留期限</th>
                    <th className={TABLE_HEADER_CELL_CLASS}>Payload</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rawPayloads.data.data.map(payload => (
                    <tr key={`${payload.run_id}:${payload.idempotency_key}`}>
                      <td className={TABLE_CELL_CLASS}>
                        {formatDate(payload.created_at)}
                      </td>
                      <td
                        className={`${TABLE_CELL_CLASS} font-mono wrap-anywhere`}
                      >
                        {payload.dataset_key}
                      </td>
                      <td className={TABLE_CELL_CLASS}>{payload.source}</td>
                      <td
                        className={`${TABLE_CELL_CLASS} font-mono wrap-anywhere`}
                      >
                        {payload.run_id}
                      </td>
                      <td className={TABLE_CELL_CLASS}>
                        {formatDate(payload.expire_at)}
                      </td>
                      <td className={TABLE_CELL_CLASS}>
                        <details className="min-w-24">
                          <summary className="cursor-pointer font-extrabold whitespace-nowrap text-accent">
                            檢視 JSON
                          </summary>
                          <pre className="mt-2 max-h-80 w-96 overflow-auto rounded-lg border border-line bg-surface-soft p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-ink wrap-anywhere sm:w-xl lg:w-2xl">
                            {JSON.stringify(payload.payload, null, 2)}
                          </pre>
                        </details>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>

      <aside className="mt-5 flex flex-wrap items-start gap-2.5 rounded-xl border border-line bg-surface px-3.5 py-3 text-xs text-muted">
        <strong className="text-ink">目前限制</strong>
        <span>
          <span className="mr-2">·</span>
          後端尚無歷史 run 趨勢 API，因此本頁只呈現即時佇列狀態。
        </span>
        <span>
          <span className="mr-2">·</span>
          Raw payload 受保留政策影響，過期資料可能無法從此查詢取得。
        </span>
      </aside>
    </main>
  )
}
