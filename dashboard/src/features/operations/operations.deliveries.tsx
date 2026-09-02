import type { ColumnDef, PaginationState } from "@tanstack/react-table"
import { Clock3 } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"

import { DataTable } from "../../components/data-table"
import type { HistoricalBackfill, MissingDelivery } from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import {
  cancelHistoricalBackfill,
  createHistoricalBackfill,
  previewHistoricalBackfill,
} from "../../lib/admin.functions"
import {
  formatDate,
  OperationsDashboardStatus,
  PageIntro,
  Panel,
  RefreshStatus,
} from "./operations.shared"
import {
  deliveriesSearchSchema,
  operationsAuditFromSearch,
  type OperationsPageSearch,
} from "./operations.search"
import {
  useOperationsDashboardQuery,
  useOperationsDashboardState,
} from "./operations.queries"

const columns: ColumnDef<MissingDelivery, unknown>[] = [
  {
    accessorKey: "dataset_key",
    header: "資料集",
    meta: { minWidth: 180, pin: "left" },
    cell: context => (
      <span className="font-mono wrap-anywhere">
        {context.getValue<string>()}
      </span>
    ),
  },
  {
    accessorKey: "source",
    header: "來源",
    meta: { minWidth: 120 },
  },
  {
    accessorKey: "expected_data_date",
    header: "預期日期",
    meta: { minWidth: 130 },
  },
  {
    accessorKey: "first_detected_at",
    header: "首次偵測",
    meta: { minWidth: 190 },
    cell: context => formatDate(context.getValue<string>()),
  },
  {
    accessorKey: "status",
    header: "狀態",
    meta: { minWidth: 100 },
  },
]

export function DeliveriesPage({
  search = deliveriesSearchSchema.parse({}),
  updateSearch = () => undefined,
  role = "viewer",
}: {
  search?: OperationsPageSearch
  updateSearch?: (next: OperationsPageSearch) => void
  role?: AdminRole
} = {}) {
  const audit = operationsAuditFromSearch(search)
  const query = useOperationsDashboardQuery("deliveries", audit)
  const state = useOperationsDashboardState(query)
  const deliveries =
    state.response?.view === "deliveries" ? state.response.deliveries : null
  const rows = deliveries?.ok ? deliveries.data : null
  const backfills =
    state.response?.view === "deliveries" ? state.response.backfills : null
  const backfillRows = backfills?.ok ? backfills.data.data : null
  const scopes =
    backfills &&
    state.response?.view === "deliveries" &&
    state.response.backfillScopes.ok
      ? state.response.backfillScopes.data.data
      : []
  const create = useServerFn(createHistoricalBackfill)
  const cancel = useServerFn(cancelHistoricalBackfill)
  const preview = useServerFn(previewHistoricalBackfill)
  const queryClient = useQueryClient()
  const [actionError, setActionError] = useState("")
  const [creating, setCreating] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [previewResult, setPreviewResult] = useState<Awaited<
    ReturnType<typeof previewHistoricalBackfill>
  > | null>(null)
  const [previewKey, setPreviewKey] = useState("")
  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ["operations", "dashboard"] })
  const previewCanCreate = Boolean(
    previewResult?.scope_valid && previewResult.days.every(day => day.valid)
  )

  useEffect(() => {
    if (!rows) return
    const totalPages = Math.max(rows.pagination.total_pages, 1)
    const page = Math.min(Math.max(search.p, 1), totalPages)
    if (page !== search.p) updateSearch({ ...search, p: page })
  }, [rows, search, updateSearch])

  const pagination: PaginationState = {
    pageIndex: Math.max(search.p - 1, 0),
    pageSize: search.ps,
  }
  const tableColumns = useMemo(() => columns, [])

  return (
    <>
      <PageIntro
        eyebrow="Completeness"
        title="缺漏交付"
        description="追蹤預期日期尚未收到的資料集交付。"
      />
      <OperationsDashboardStatus state={state} />
      <Panel
        eyebrow="Open alerts"
        title="未解決的交付缺漏"
        icon={<Clock3 size={19} />}
        result={deliveries}
        loading={state.initialLoading}
      >
        {rows ? (
          <>
            <RefreshStatus
              pending={state.pending}
              label="正在更新交付缺漏…目前資料仍可使用。"
            />
            <DataTable
              ariaLabel="未解決的交付缺漏"
              caption="未解決的交付缺漏"
              columns={tableColumns}
              data={rows.data}
              emptyState="目前沒有未解決的交付缺漏。"
              fillAvailableWidth
              getRowId={row => row.alert_id}
              isRefreshing={state.pending && !state.initialLoading}
              manualPagination
              pageCount={Math.max(rows.pagination.total_pages, 1)}
              pagination={pagination}
              pageSizeOptions={[25, 50, 100]}
              rowCount={rows.pagination.total_records}
              onPaginationChange={next => {
                const nextState =
                  typeof next === "function" ? next(pagination) : next
                if (nextState.pageSize !== search.ps) {
                  updateSearch({
                    ...search,
                    p: 1,
                    ps: nextState.pageSize as OperationsPageSearch["ps"],
                  })
                  return
                }
                if (nextState.pageIndex !== pagination.pageIndex) {
                  updateSearch({ ...search, p: nextState.pageIndex + 1 })
                }
              }}
            />
          </>
        ) : null}
      </Panel>
      {role !== "viewer" && (
        <Panel
          eyebrow="Historical"
          title="供應商歷史回補"
          icon={<Clock3 size={19} />}
          result={backfills}
          loading={state.initialLoading}
        >
          <form
            className="mb-4 grid gap-2 md:grid-cols-5"
            onSubmit={event => {
              event.preventDefault()
              if (!confirmed) {
                setActionError("請先確認同一 provider 會依日期順序執行。")
                return
              }
              const form = new FormData(event.currentTarget)
              const currentKey = [
                form.get("provider"),
                form.get("dataset"),
                form.get("start"),
                form.get("end"),
              ].join(":")
              if (
                !previewResult ||
                previewKey !== currentKey ||
                !previewCanCreate
              ) {
                setActionError("範圍內所有日期都必須通過交易日驗證。")
                return
              }
              setCreating(true)
              setActionError("")
              void create({
                data: {
                  provider: String(form.get("provider") || ""),
                  datasetKey: String(form.get("dataset") || ""),
                  startDate: String(form.get("start") || ""),
                  endDate: String(form.get("end") || ""),
                  requestKey: crypto.randomUUID(),
                },
              })
                .then(refresh)
                .catch(() => setActionError("建立回補請求失敗。"))
                .finally(() => setCreating(false))
            }}
          >
            <input
              required
              name="provider"
              placeholder="provider"
              list="historical-provider-scopes"
              className="rounded border bg-background px-2 py-1"
              onChange={() => {
                setPreviewResult(null)
                setConfirmed(false)
              }}
            />
            <input
              required
              name="dataset"
              placeholder="dataset_key"
              list="historical-dataset-scopes"
              className="rounded border bg-background px-2 py-1"
              onChange={() => {
                setPreviewResult(null)
                setConfirmed(false)
              }}
            />
            <datalist id="historical-provider-scopes">
              {scopes.map(scope => (
                <option
                  key={`${scope.provider}:${scope.dataset_key}`}
                  value={scope.provider}
                >
                  {scope.provider}／{scope.dataset_key}（{scope.market}）
                </option>
              ))}
            </datalist>
            <datalist id="historical-dataset-scopes">
              {scopes.map(scope => (
                <option
                  key={`${scope.dataset_key}:${scope.provider}`}
                  value={scope.dataset_key}
                >
                  {scope.provider}／{scope.market}
                </option>
              ))}
            </datalist>
            <input
              required
              name="start"
              type="date"
              aria-label="回補起始日期"
              className="rounded border bg-background px-2 py-1"
              onChange={() => {
                setPreviewResult(null)
                setConfirmed(false)
              }}
            />
            <input
              required
              name="end"
              type="date"
              aria-label="回補結束日期"
              className="rounded border bg-background px-2 py-1"
              onChange={() => {
                setPreviewResult(null)
                setConfirmed(false)
              }}
            />
            <button
              type="button"
              className="rounded border px-3 py-1"
              onClick={event => {
                const form = event.currentTarget.form
                if (!form) return
                const data = new FormData(form)
                const input = {
                  provider: String(data.get("provider") || ""),
                  datasetKey: String(data.get("dataset") || ""),
                  startDate: String(data.get("start") || ""),
                  endDate: String(data.get("end") || ""),
                }
                const key = [
                  input.provider,
                  input.datasetKey,
                  input.startDate,
                  input.endDate,
                ].join(":")
                setActionError("")
                void preview({ data: input })
                  .then(value => {
                    setPreviewResult(value)
                    setPreviewKey(key)
                  })
                  .catch(() => setActionError("交易日驗證失敗。"))
              }}
            >
              驗證交易日
            </button>
            {previewResult && (
              <div
                className="col-span-full rounded bg-surface-soft p-2 text-sm"
                role="status"
              >
                <p>
                  {previewResult.scope_valid
                    ? previewCanCreate
                      ? "範圍有效；所有日期都會建立工作項目。"
                      : "範圍含無效日期；整個回補請求將被拒絕。"
                    : `範圍無效：${previewResult.scope_reason ?? "unknown"}`}
                </p>
                <p className="text-muted">
                  {previewResult.days
                    .map(
                      day =>
                        `${day.trade_date} ${day.valid ? "可執行" : `拒絕：${day.reason}`}`
                    )
                    .join("；")}
                </p>
              </div>
            )}
            <label className="col-span-full flex items-center gap-2 text-sm text-muted">
              <input
                checked={confirmed}
                onChange={event => setConfirmed(event.target.checked)}
                type="checkbox"
              />
              我確認同一 provider
              的日期會依序執行；任一日期終止失敗會停止後續日期。
            </label>
            <button
              disabled={creating || !confirmed || !previewCanCreate}
              className="rounded bg-primary px-3 py-1 text-primary-foreground disabled:opacity-50"
              type="submit"
            >
              {creating ? "建立中…" : "建立回補"}
            </button>
          </form>
          {actionError && (
            <p className="mb-3 text-sm text-destructive" role="alert">
              {actionError}
            </p>
          )}
          {backfillRows ? (
            <div className="space-y-2">
              {backfillRows.map((row: HistoricalBackfill) => (
                <details
                  key={row.request_id}
                  className="rounded border p-2 text-sm"
                >
                  <summary className="flex cursor-pointer flex-wrap items-center gap-2">
                    <span className="font-mono">
                      {row.provider}/{row.dataset_key}
                    </span>
                    <span>
                      {row.start_date} – {row.end_date}
                    </span>
                    <span className="rounded bg-surface-soft px-2">
                      {row.status}
                    </span>
                    <span className="text-muted">
                      {
                        row.items.filter(item => item.status === "completed")
                          .length
                      }
                      /{row.items.length} 完成
                    </span>
                    {row.failure_code && (
                      <span className="text-destructive">
                        {row.failure_code}
                      </span>
                    )}
                  </summary>
                  <ul className="mt-2 space-y-1 border-t pt-2 font-mono text-xs text-muted">
                    {row.items.map(item => (
                      <li key={item.item_id}>
                        {item.trade_date} · {item.status} · run:{" "}
                        {item.run_id ?? "—"}
                        {item.failure_code ? ` · ${item.failure_code}` : ""}
                        {item.failure_message
                          ? ` · ${item.failure_message}`
                          : ""}
                      </li>
                    ))}
                  </ul>
                  {(row.status === "queued" || row.status === "running") && (
                    <button
                      className="ml-auto text-destructive underline"
                      onClick={() =>
                        void cancel({ data: { requestId: row.request_id } })
                          .then(refresh)
                          .catch(() => setActionError("取消回補請求失敗。"))
                      }
                    >
                      取消
                    </button>
                  )}
                </details>
              ))}
            </div>
          ) : null}
        </Panel>
      )}
    </>
  )
}
