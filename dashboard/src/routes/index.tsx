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

function Panel({
  title,
  eyebrow,
  icon,
  result,
  children,
}: {
  title: string
  eyebrow: string
  icon: React.ReactNode
  result: PanelResult<unknown>
  children: React.ReactNode
}) {
  return (
    <section className="panel">
      <header className="panel-header">
        <span className="panel-icon">{icon}</span>
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
        </div>
      </header>
      {result.ok ? (
        children
      ) : (
        <div className="state error-state">
          <TriangleAlert size={18} />
          <span>{result.error}</span>
        </div>
      )}
    </section>
  )
}

function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="state empty-state">
      <CheckCircle2 size={18} />
      <span>{children}</span>
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
  const [pending, setPending] = useState(false)
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

  const unavailable: PanelResult<unknown> = {
    ok: false,
    error: "連線後顯示資料",
  }
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
      ? "idle"
      : successfulPanels === panelResults.length
        ? "healthy"
        : successfulPanels === 0
          ? "failed"
          : "degraded"
  const connectionLabel = {
    idle: "尚未連線",
    healthy: "連線正常",
    degraded: "部分服務異常",
    failed: "連線失敗",
  }[connectionState]

  return (
    <main className="console-shell">
      <section className="hero">
        <div>
          <p className="eyebrow">FinDB Operations / Read only</p>
          <h1>資料導入營運台</h1>
          <p className="hero-copy">
            集中檢查導入穩定度、資料完整性與修正稽核，所有操作皆為唯讀。
          </p>
        </div>
        <div className="operator-panel">
          <div>
            <span>已登入</span>
            <strong>{username}</strong>
            <small>Admin key 僅由 Dashboard server 讀取</small>
          </div>
          <div className="operator-actions">
            <button
              type="button"
              onClick={() => void refresh(filters)}
              disabled={pending}
            >
              {pending ? (
                <RefreshCw className="spin" size={17} />
              ) : (
                <Wifi size={17} />
              )}
              重新整理
            </button>
            <button
              className="secondary-button"
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
        <div className="global-error" role="alert">
          <AlertTriangle size={18} />
          {error}
        </div>
      )}

      <div className="status-strip">
        <span className={`status-dot ${connectionState}`} />
        <strong>{connectionLabel}</strong>
        <span>
          {data
            ? `${successfulPanels}/${panelResults.length} 個資料來源成功 · 最後更新 ${formatDate(data.fetchedAt)}`
            : "正在載入營運資料"}
        </span>
        {data && (
          <button
            className="text-button"
            type="button"
            onClick={() => void refresh(filters)}
            disabled={pending}
          >
            <RefreshCw className={pending ? "spin" : ""} size={15} />
            手動更新
          </button>
        )}
      </div>
      {connectionState === "failed" && (
        <div className="global-error" role="alert">
          <AlertTriangle size={18} />
          所有 Admin API 查詢均失敗，請確認後端服務與 Dashboard server 設定。
        </div>
      )}
      {connectionState === "degraded" && (
        <div className="global-warning" role="status">
          <AlertTriangle size={18} />
          部分資料來源暫時無法取得；其餘成功面板仍為有效結果。
        </div>
      )}

      <div className="panel-grid">
        <Panel
          eyebrow="Ingestion stability"
          title="佇列與 Worker"
          icon={<Database size={19} />}
          result={data?.queue ?? unavailable}
        >
          {data?.queue.ok && (
            <div className="metric-grid">
              <div className="metric">
                <span>排隊中</span>
                <strong>{data.queue.data.counts.queued ?? 0}</strong>
              </div>
              <div className="metric">
                <span>處理中</span>
                <strong>{data.queue.data.counts.processing ?? 0}</strong>
              </div>
              <div className="metric">
                <span>重試耗盡</span>
                <strong>{data.queue.data.retry_exhausted}</strong>
              </div>
              <div className="metric">
                <span>過期租約</span>
                <strong>{data.queue.data.expired_leases}</strong>
              </div>
              <div className="metric wide">
                <span>Worker heartbeat</span>
                <strong>
                  {formatAge(data.queue.data.worker_heartbeat_age_seconds)}
                </strong>
                <small>
                  {formatDate(data.queue.data.last_worker_heartbeat_at)}
                </small>
              </div>
              <div className="metric wide">
                <span>未發布 outbox</span>
                <strong>{data.queue.data.unpublished_outbox}</strong>
              </div>
            </div>
          )}
        </Panel>

        <Panel
          eyebrow="Completeness"
          title="缺漏交付"
          icon={<Clock3 size={19} />}
          result={data?.deliveries ?? unavailable}
        >
          {data?.deliveries.ok &&
            (data.deliveries.data.data.length === 0 ? (
              <EmptyState>目前沒有未解決的交付缺漏</EmptyState>
            ) : (
              <>
                <p className="result-count">
                  共 {data.deliveries.data.pagination.total_records} 筆，顯示前{" "}
                  {data.deliveries.data.data.length} 筆
                </p>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>資料集</th>
                        <th>來源</th>
                        <th>預期日期</th>
                        <th>首次偵測</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.deliveries.data.data.map(alert => (
                        <tr key={alert.alert_id}>
                          <td className="mono">{alert.dataset_key}</td>
                          <td>{alert.source}</td>
                          <td>{alert.expected_data_date}</td>
                          <td>{formatDate(alert.first_detected_at)}</td>
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
          result={data?.issues ?? unavailable}
        >
          {data?.issues.ok &&
            (data.issues.data.data.length === 0 ? (
              <EmptyState>目前沒有未解決的資料品質問題</EmptyState>
            ) : (
              <>
                <p className="result-count">
                  共 {data.issues.data.pagination.total_records} 筆，顯示前{" "}
                  {data.issues.data.data.length} 筆
                </p>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>嚴重度</th>
                        <th>類型</th>
                        <th>交易日</th>
                        <th>說明</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.issues.data.data.map(issue => (
                        <tr key={issue.id}>
                          <td>
                            <span className={`severity ${issue.severity}`}>
                              {issue.severity}
                            </span>
                          </td>
                          <td className="mono">{issue.issue_type}</td>
                          <td>{issue.trade_date ?? "—"}</td>
                          <td>{issue.description ?? "—"}</td>
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
          result={data?.corrections ?? unavailable}
        >
          {data?.corrections.ok &&
            (data.corrections.data.data.length === 0 ? (
              <EmptyState>目前沒有修正紀錄</EmptyState>
            ) : (
              <>
                <p className="result-count">
                  共 {data.corrections.data.pagination.total_records} 筆，顯示前{" "}
                  {data.corrections.data.data.length} 筆
                </p>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>時間</th>
                        <th>資料表</th>
                        <th>修正者</th>
                        <th>原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.corrections.data.data.map(correction => (
                        <tr key={correction.id}>
                          <td>{formatDate(correction.created_at)}</td>
                          <td className="mono">{correction.table_name}</td>
                          <td>{correction.corrected_by}</td>
                          <td>{correction.correction_reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            ))}
        </Panel>
      </div>

      <section className="panel audit-panel">
        <header className="panel-header">
          <span className="panel-icon">
            <Search size={19} />
          </span>
          <div>
            <p className="eyebrow">Raw payload retrieval</p>
            <h2>原始資料稽核查詢</h2>
          </div>
        </header>
        <form className="filter-grid" onSubmit={submitAudit}>
          <label>
            Dataset key
            <input
              value={filters.datasetKey}
              onChange={event =>
                setFilters({ ...filters, datasetKey: event.target.value })
              }
            />
          </label>
          <label>
            Run ID
            <input
              value={filters.runId}
              onChange={event =>
                setFilters({ ...filters, runId: event.target.value })
              }
            />
          </label>
          <label>
            起始日期
            <input
              type="date"
              value={filters.dateFrom}
              onChange={event =>
                setFilters({ ...filters, dateFrom: event.target.value })
              }
            />
          </label>
          <label>
            結束日期
            <input
              type="date"
              value={filters.dateTo}
              onChange={event =>
                setFilters({ ...filters, dateTo: event.target.value })
              }
            />
          </label>
          <button type="submit" disabled={pending}>
            <Search size={16} /> 查詢
          </button>
        </form>
        {!data ? (
          <div className="state empty-state">連線後可查詢 raw payload</div>
        ) : !data.rawPayloads.ok ? (
          <div className="state error-state">{data.rawPayloads.error}</div>
        ) : data.rawPayloads.data.data.length === 0 ? (
          <EmptyState>查無符合條件的原始資料</EmptyState>
        ) : (
          <>
            <p className="result-count">
              共 {data.rawPayloads.data.pagination.total_records} 筆，顯示前{" "}
              {data.rawPayloads.data.data.length} 筆
            </p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>建立時間</th>
                    <th>Dataset</th>
                    <th>來源</th>
                    <th>Run ID</th>
                    <th>保留期限</th>
                    <th>Payload</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rawPayloads.data.data.map(payload => (
                    <tr key={`${payload.run_id}:${payload.idempotency_key}`}>
                      <td>{formatDate(payload.created_at)}</td>
                      <td className="mono">{payload.dataset_key}</td>
                      <td>{payload.source}</td>
                      <td className="mono">{payload.run_id}</td>
                      <td>{formatDate(payload.expire_at)}</td>
                      <td>
                        <details className="payload-review">
                          <summary>檢視 JSON</summary>
                          <pre>{JSON.stringify(payload.payload, null, 2)}</pre>
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

      <aside className="limitations">
        <strong>目前限制</strong>
        <span>後端尚無歷史 run 趨勢 API，因此本頁只呈現即時佇列狀態。</span>
        <span>Raw payload 受保留政策影響，過期資料可能無法從此查詢取得。</span>
      </aside>
    </main>
  )
}
