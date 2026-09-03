import type { ColumnDef, PaginationState } from "@tanstack/react-table"
import { AlertTriangle, ArrowRight, CheckCircle2, Clock3 } from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"

import { DataTable, DataTablePagination } from "../../components/data-table"
import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
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
  deliveriesAuditFromSearch,
  type DeliveriesPageSearch,
} from "./operations.search"
import {
  useOperationsDashboardQuery,
  useOperationsDashboardState,
} from "./operations.queries"

export type DeliveriesSearchUpdate =
  | DeliveriesPageSearch
  | ((previous: DeliveriesPageSearch) => DeliveriesPageSearch)

const backfillScopeReasonLabels: Record<string, string> = {
  date_order_invalid: "結束日期不可早於起始日期",
  outside_latest_31_days: "日期範圍必須在最近 31 天內，且不可晚於今日",
  dataset_inactive: "資料集尚未啟用",
  provider_dataset_unsupported: "供應商不支援此資料集的歷史回補",
  provider_scope_inactive: "供應商與資料集的回補範圍尚未啟用",
}

function formatBackfillScopeReason(reason: string | null) {
  const code = reason ?? "unknown"
  return `${backfillScopeReasonLabels[code] ?? "未知錯誤"}（${code}）`
}

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

function BackfillStepHeader({
  step,
  title,
  description,
  status,
}: {
  step: number
  title: string
  description: string
  status: "active" | "complete" | "pending"
}) {
  return (
    <div className="flex items-start gap-3">
      <span
        className={
          status === "complete"
            ? "inline-flex size-7 shrink-0 items-center justify-center rounded-full border border-accent/20 bg-accent-soft text-xs font-bold text-accent"
            : status === "active"
              ? "inline-flex size-7 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-bold text-white"
              : "inline-flex size-7 shrink-0 items-center justify-center rounded-full border border-line bg-surface-soft text-xs font-bold text-muted"
        }
        aria-hidden="true"
      >
        {status === "complete" ? <CheckCircle2 size={15} /> : step}
      </span>
      <div>
        <h3 className="m-0 text-sm font-bold text-ink">{title}</h3>
        <p className="mt-1 mb-0 text-xs leading-relaxed text-muted">
          {description}
        </p>
      </div>
    </div>
  )
}

export function DeliveriesPage({
  search = deliveriesSearchSchema.parse({}),
  updateSearch = () => undefined,
  role = "viewer",
}: {
  search?: DeliveriesPageSearch
  updateSearch?: (next: DeliveriesSearchUpdate) => void
  role?: AdminRole
} = {}) {
  const audit = deliveriesAuditFromSearch(search)
  const query = useOperationsDashboardQuery("deliveries", audit)
  const state = useOperationsDashboardState(query)
  const deliveries =
    state.response?.view === "deliveries" ? state.response.deliveries : null
  const rows = deliveries?.ok ? deliveries.data : null
  const backfills =
    state.response?.view === "deliveries" ? state.response.backfills : null
  const backfillRows = backfills?.ok ? backfills.data.data : null
  const backfillPagination = backfills?.ok ? backfills.data.pagination : null
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
  const [previewing, setPreviewing] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [previewResult, setPreviewResult] = useState<Awaited<
    ReturnType<typeof previewHistoricalBackfill>
  > | null>(null)
  const [previewKey, setPreviewKey] = useState("")
  const previewRevision = useRef(0)
  const [backfillDraft, setBackfillDraft] = useState({
    provider: "",
    dataset: "",
    start: "",
    end: "",
  })
  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ["operations", "dashboard"] })
  const openDays = previewResult?.days.filter(day => day.valid) ?? []
  const closedDays =
    previewResult?.days.filter(day => day.reason === "market_closed") ?? []
  const unpublishedDays =
    previewResult?.days.filter(day => day.reason === "calendar_unpublished") ??
    []
  const previewCanCreate = Boolean(
    previewResult?.scope_valid &&
    openDays.length > 0 &&
    unpublishedDays.length === 0
  )
  const draftComplete = Object.values(backfillDraft).every(Boolean)

  useEffect(() => {
    const deliveryPage = rows
      ? Math.min(
          Math.max(search.p, 1),
          Math.max(rows.pagination.total_pages, 1)
        )
      : search.p
    const historicalPage = backfillPagination
      ? Math.min(
          Math.max(search.bp, 1),
          Math.max(backfillPagination.total_pages, 1)
        )
      : search.bp
    if (deliveryPage !== search.p || historicalPage !== search.bp) {
      updateSearch({ ...search, p: deliveryPage, bp: historicalPage })
    }
  }, [backfillPagination, rows, search, updateSearch])

  const pagination: PaginationState = {
    pageIndex: Math.max(search.p - 1, 0),
    pageSize: search.ps,
  }
  const historicalPagination: PaginationState = {
    pageIndex: Math.max(search.bp - 1, 0),
    pageSize: search.bps,
  }
  const invalidatePreview = useCallback(() => {
    previewRevision.current += 1
    setPreviewResult(null)
    setPreviewKey("")
    setConfirmed(false)
    setPreviewing(false)
  }, [])
  const prefillBackfill = useCallback(
    (row: MissingDelivery) => {
      setBackfillDraft({
        provider: row.source,
        dataset: row.dataset_key,
        start: row.expected_data_date,
        end: row.expected_data_date,
      })
      invalidatePreview()
      setActionError("")
    },
    [invalidatePreview]
  )
  const updateBackfillDraft = (
    field: keyof typeof backfillDraft,
    value: string
  ) => {
    setBackfillDraft(current => ({ ...current, [field]: value }))
    invalidatePreview()
    setActionError("")
  }
  const tableColumns = useMemo<ColumnDef<MissingDelivery, unknown>[]>(
    () =>
      role === "viewer"
        ? columns
        : [
            ...columns,
            {
              id: "backfill",
              header: "回補",
              meta: { minWidth: 120 },
              cell: context => {
                const row = context.row.original
                return (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    aria-label={`帶入 ${row.dataset_key} ${row.expected_data_date} 回補參數`}
                    onClick={() => prefillBackfill(row)}
                  >
                    帶入回補
                    <ArrowRight aria-hidden="true" />
                  </Button>
                )
              },
            },
          ],
    [prefillBackfill, role]
  )

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
              paginationAriaLabel="交付缺漏分頁"
              rowCount={rows.pagination.total_records}
              onPaginationChange={next => {
                const nextState =
                  typeof next === "function" ? next(pagination) : next
                if (nextState.pageSize !== search.ps) {
                  updateSearch({
                    ...search,
                    p: 1,
                    ps: nextState.pageSize as DeliveriesPageSearch["ps"],
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
          className="mt-5"
          eyebrow="Historical"
          title="供應商歷史回補"
          icon={<Clock3 size={19} />}
          result={backfills}
          loading={state.initialLoading}
        >
          <form
            className="mb-6 overflow-hidden rounded-xl border border-line bg-surface"
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
                .then(() => {
                  updateSearch(current => ({ ...current, bp: 1 }))
                  return refresh()
                })
                .catch(() => setActionError("建立回補請求失敗。"))
                .finally(() => setCreating(false))
            }}
          >
            <section className="grid gap-4 p-4 sm:p-5">
              <BackfillStepHeader
                step={1}
                title="選擇回補範圍"
                description="可從上方缺漏列帶入，或手動指定已啟用的 provider、dataset 與日期。"
                status={draftComplete ? "complete" : "active"}
              />
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div className="grid gap-1.5">
                  <Label htmlFor="backfill-provider">供應商</Label>
                  <Input
                    required
                    id="backfill-provider"
                    name="provider"
                    placeholder="provider"
                    list="historical-provider-scopes"
                    value={backfillDraft.provider}
                    onChange={event =>
                      updateBackfillDraft("provider", event.target.value)
                    }
                  />
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor="backfill-dataset">資料集</Label>
                  <Input
                    required
                    id="backfill-dataset"
                    name="dataset"
                    placeholder="dataset_key"
                    list="historical-dataset-scopes"
                    value={backfillDraft.dataset}
                    onChange={event =>
                      updateBackfillDraft("dataset", event.target.value)
                    }
                  />
                </div>
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
                <div className="grid gap-1.5">
                  <Label htmlFor="backfill-start">起始日期</Label>
                  <Input
                    required
                    id="backfill-start"
                    name="start"
                    type="date"
                    aria-label="回補起始日期"
                    value={backfillDraft.start}
                    onChange={event =>
                      updateBackfillDraft("start", event.target.value)
                    }
                  />
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor="backfill-end">結束日期</Label>
                  <Input
                    required
                    id="backfill-end"
                    name="end"
                    type="date"
                    aria-label="回補結束日期"
                    value={backfillDraft.end}
                    onChange={event =>
                      updateBackfillDraft("end", event.target.value)
                    }
                  />
                </div>
              </div>
            </section>

            <section className="grid gap-4 border-t border-line bg-surface-soft/50 p-4 sm:p-5">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <BackfillStepHeader
                  step={2}
                  title="驗證交易日"
                  description="先確認 provider scope、日期範圍與每個交易日都可執行。"
                  status={
                    previewCanCreate
                      ? "complete"
                      : draftComplete
                        ? "active"
                        : "pending"
                  }
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={!draftComplete || previewing}
                  aria-busy={previewing}
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
                    const revision = ++previewRevision.current
                    setPreviewing(true)
                    setPreviewResult(null)
                    setPreviewKey("")
                    setConfirmed(false)
                    setActionError("")
                    void preview({ data: input })
                      .then(value => {
                        if (previewRevision.current !== revision) return
                        setPreviewResult(value)
                        setPreviewKey(key)
                      })
                      .catch(() => {
                        if (previewRevision.current !== revision) return
                        setActionError("交易日驗證失敗。")
                      })
                      .finally(() => {
                        if (previewRevision.current !== revision) return
                        setPreviewing(false)
                      })
                  }}
                >
                  {previewing ? "驗證中…" : "驗證交易日"}
                </Button>
              </div>
              {previewResult ? (
                <Alert
                  variant={
                    previewCanCreate
                      ? closedDays.length > 0
                        ? "warning"
                        : "success"
                      : "destructive"
                  }
                  role="status"
                >
                  {previewCanCreate ? (
                    <CheckCircle2 aria-hidden="true" />
                  ) : (
                    <AlertTriangle aria-hidden="true" />
                  )}
                  <AlertTitle>
                    {previewResult.scope_valid
                      ? previewCanCreate
                        ? closedDays.length > 0
                          ? "範圍有效；休市日期會略過，只為開市日期建立工作項目。"
                          : "範圍有效；所有日期都會建立工作項目。"
                        : unpublishedDays.length > 0
                          ? "範圍含未發布交易日；整個回補請求將被拒絕。"
                          : "範圍無可執行交易日；整個回補請求將被拒絕。"
                      : `範圍無效：${formatBackfillScopeReason(previewResult.scope_reason)}`}
                  </AlertTitle>
                  <AlertDescription>
                    可建立 {openDays.length} 個開市日期工作項目；略過{" "}
                    {closedDays.length} 個休市日期。
                    {closedDays.length > 0 && (
                      <>
                        略過日期：
                        {closedDays.map(day => day.trade_date).join("、")}。
                      </>
                    )}
                    {unpublishedDays.length > 0 && (
                      <>
                        未發布日期：
                        {unpublishedDays.map(day => day.trade_date).join("、")}
                        ；整個請求會被拒絕。
                      </>
                    )}
                  </AlertDescription>
                </Alert>
              ) : (
                <div
                  className="rounded-lg border border-dashed border-line bg-surface px-4 py-3 text-sm text-muted"
                  role="status"
                  aria-live="polite"
                >
                  {previewing
                    ? "正在驗證回補範圍與交易日…"
                    : draftComplete
                      ? "參數已齊全，請執行交易日驗證。"
                      : "請先完成所有回補參數。"}
                </div>
              )}
            </section>

            <section className="grid gap-4 border-t border-line p-4 sm:p-5">
              <BackfillStepHeader
                step={3}
                title="確認並建立"
                description="確認執行規則後才會送出；建立請求不會手動關閉缺漏 alert。"
                status={
                  confirmed
                    ? "complete"
                    : previewCanCreate
                      ? "active"
                      : "pending"
                }
              />
              <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-line bg-surface-soft p-3 text-sm text-ink">
                <input
                  className="mt-0.5 size-4 shrink-0 accent-accent"
                  checked={confirmed}
                  disabled={!previewCanCreate || previewing}
                  onChange={event => setConfirmed(event.target.checked)}
                  type="checkbox"
                />
                <span className="leading-relaxed">
                  我確認同一 provider
                  的日期會依序執行；任一日期終止失敗會停止後續日期。
                </span>
              </label>
              {actionError && (
                <Alert variant="destructive" role="alert">
                  <AlertTriangle aria-hidden="true" />
                  <AlertDescription>{actionError}</AlertDescription>
                </Alert>
              )}
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                <p className="m-0 text-xs leading-relaxed text-muted">
                  {previewCanCreate
                    ? `驗證已通過；將建立 ${openDays.length} 個開市日期工作項目，勾選確認後即可送出原始範圍。`
                    : "完成前兩個步驟後才能建立回補。"}
                </p>
                <Button
                  disabled={
                    creating || previewing || !confirmed || !previewCanCreate
                  }
                  className="w-full sm:w-auto"
                  size="lg"
                  type="submit"
                >
                  {creating ? "建立中…" : "建立回補"}
                  {!creating && <ArrowRight aria-hidden="true" />}
                </Button>
              </div>
            </section>
          </form>
          <div className="mb-3">
            <h3 className="m-0 text-sm font-bold text-ink">回補請求紀錄</h3>
            <p className="mt-1 mb-0 text-xs text-muted">
              追蹤已建立請求的日期、執行狀態與 run ID。
            </p>
          </div>
          {backfillRows ? (
            <>
              {backfillRows.length > 0 ? (
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
                            row.items.filter(
                              item => item.status === "completed"
                            ).length
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
                      {(row.status === "queued" ||
                        row.status === "running") && (
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
              ) : (
                <p className="m-0 rounded-lg border border-dashed border-line p-4 text-sm text-muted">
                  尚無歷史回補請求。
                </p>
              )}
              {backfillPagination && (
                <DataTablePagination
                  pageCount={Math.max(backfillPagination.total_pages, 1)}
                  pageIndex={historicalPagination.pageIndex}
                  pageSize={historicalPagination.pageSize}
                  ariaLabel="歷史回補請求分頁"
                  pageSizeOptions={[25, 50, 100]}
                  rowCount={backfillPagination.total_records}
                  onPageSizeChange={pageSize => {
                    if (pageSize !== search.bps) {
                      updateSearch({
                        ...search,
                        bp: 1,
                        bps: pageSize as DeliveriesPageSearch["bps"],
                      })
                    }
                  }}
                  onPageChange={pageIndex => {
                    if (pageIndex !== historicalPagination.pageIndex) {
                      updateSearch({ ...search, bp: pageIndex + 1 })
                    }
                  }}
                />
              )}
            </>
          ) : null}
        </Panel>
      )}
    </>
  )
}
