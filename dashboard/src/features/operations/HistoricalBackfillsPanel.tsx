import type { ColumnDef, PaginationState } from "@tanstack/react-table"
import { AlertTriangle, ArrowRight, CheckCircle2, Clock3 } from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"

import { DataTable } from "../../components/data-table"
import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import { Badge } from "../../components/ui/badge"
import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select"
import type { HistoricalBackfill, MissingDelivery } from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import {
  cancelHistoricalBackfill,
  createHistoricalBackfill,
  previewHistoricalBackfill,
} from "../../lib/admin.functions"
import { formatDate, Panel } from "./operations.shared"
import {
  deliveriesSearchSchema,
  type DeliveriesPageSearch,
} from "./operations.search"
import { useDeliveryResource } from "./delivery.queries"
import { useOperationsRefresh } from "../../components/OperationsRefresh"
import { QueryStatus } from "../../components/AsyncState"

export type DeliveriesSearchUpdate =
  | DeliveriesPageSearch
  | ((previous: DeliveriesPageSearch) => DeliveriesPageSearch)

const backfillScopeReasonLabels: Record<string, string> = {
  date_order_invalid: "結束日期不可早於起始日期",
  outside_latest_31_days: "日期範圍必須在最近 31 天內，且不可晚於今日",
  dataset_inactive: "資料集尚未啟用",
  provider_dataset_unsupported: "供應商不支援此資料集的歷史回補",
  provider_scope_inactive: "供應商與資料集的回補範圍尚未啟用",
  calendar_unpublished: "範圍內有日期還沒有已發布的交易所日曆",
}

function formatBackfillScopeReason(reason: string | null) {
  const code = reason ?? "unknown"
  return `${backfillScopeReasonLabels[code] ?? "未知錯誤"}（${code}）`
}

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
              ? "inline-flex size-7 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-bold text-accent-ink"
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

type BackfillItem = HistoricalBackfill["items"][number]

const backfillStatusLabels: Record<HistoricalBackfill["status"], string> = {
  queued: "排隊中",
  running: "執行中",
  completed: "已完成",
  partial: "部分完成",
  failed: "失敗",
  cancelled: "已取消",
}

function BackfillStatusBadge({
  status,
}: {
  status: HistoricalBackfill["status"]
}) {
  return (
    <Badge
      variant={
        status === "failed"
          ? "destructive"
          : status === "partial"
            ? "warning"
            : status === "completed"
              ? "secondary"
              : "outline"
      }
    >
      {backfillStatusLabels[status]}
    </Badge>
  )
}

function backfillItemStatusLabel(status: string) {
  if (status === "skipped_calendar") return "依設定略過"
  if (status === "partial") return "部分完成，保留缺漏"
  return status
}

const backfillItemColumns: ColumnDef<BackfillItem, unknown>[] = [
  { accessorKey: "trade_date", header: "交易日", meta: { width: 120 } },
  {
    accessorKey: "status",
    header: "狀態",
    meta: { minWidth: 150 },
    cell: context => backfillItemStatusLabel(context.getValue<string>()),
  },
  {
    accessorKey: "run_id",
    header: "Run ID",
    meta: { minWidth: 300 },
    cell: context => (
      <span className="font-mono">{context.getValue<string>() ?? "—"}</span>
    ),
  },
  {
    id: "failure",
    header: "失敗原因",
    meta: { minWidth: 220, wrap: true },
    cell: ({ row }) =>
      row.original.failure_code || row.original.failure_message ? (
        <span className="text-danger">
          {[row.original.failure_code, row.original.failure_message]
            .filter(Boolean)
            .join(" · ")}
        </span>
      ) : (
        "—"
      ),
  },
  {
    id: "unlisted",
    header: "清單外有資料",
    meta: { minWidth: 220, wrap: true },
    cell: ({ row }) =>
      row.original.unlisted_symbols?.length ? (
        <span className="break-all">
          {row.original.unlisted_symbols.length} 檔：
          {row.original.unlisted_symbols.join("、")}
        </span>
      ) : (
        "—"
      ),
  },
]

function BackfillItemsTable({ items }: { items: BackfillItem[] }) {
  return (
    <DataTable
      ariaLabel="回補日期項目"
      columns={backfillItemColumns}
      data={items}
      getRowId={item => item.item_id}
      refreshingState={null}
      emptyState="此請求沒有日期項目。"
      fillAvailableWidth
      viewportClassName="max-h-80"
    />
  )
}

const baseBackfillColumns: ColumnDef<HistoricalBackfill, unknown>[] = [
  {
    id: "scope",
    header: "供應商／資料集",
    meta: { minWidth: 200, pin: "left" },
    cell: ({ row }) => (
      <span className="font-mono">
        {row.original.provider}/{row.original.dataset_key}
      </span>
    ),
  },
  {
    id: "range",
    header: "日期範圍",
    meta: { minWidth: 210 },
    cell: ({ row }) => `${row.original.start_date} – ${row.original.end_date}`,
  },
  {
    accessorKey: "status",
    header: "狀態",
    meta: { width: 100 },
    cell: context => (
      <BackfillStatusBadge
        status={context.getValue<HistoricalBackfill["status"]>()}
      />
    ),
  },
  {
    id: "progress",
    header: "進度",
    meta: { minWidth: 160 },
    cell: ({ row }) => {
      const items = row.original.items
      const completed = items.filter(item => item.status === "completed")
      const partial = items.filter(item => item.status === "partial")
      return (
        <span>
          {completed.length}/{items.length} 完成
          {partial.length > 0 && (
            <span className="ml-2 text-muted">{partial.length} 日仍有缺漏</span>
          )}
        </span>
      )
    },
  },
  {
    accessorKey: "failure_code",
    header: "失敗代碼",
    meta: { minWidth: 140 },
    cell: context =>
      context.getValue<string | null>() ? (
        <span className="font-mono text-danger">
          {context.getValue<string>()}
        </span>
      ) : (
        "—"
      ),
  },
  {
    accessorKey: "created_at",
    header: "建立時間",
    meta: { minWidth: 170 },
    cell: context => formatDate(context.getValue<string>()),
  },
  {
    accessorKey: "created_by",
    header: "建立者",
    meta: { minWidth: 120 },
  },
]

export function HistoricalBackfillsPanel({
  search = deliveriesSearchSchema.parse({}),
  updateSearch = () => undefined,
  role = "viewer",
  active = true,
  seed,
}: {
  search?: DeliveriesPageSearch
  updateSearch?: (next: DeliveriesSearchUpdate) => void
  role?: AdminRole
  active?: boolean
  seed?: MissingDelivery | undefined
} = {}) {
  const query = useDeliveryResource(
    { resource: "backfills", page: search.bp, pageSize: search.bps },
    active && role !== "viewer"
  )
  const scopesQuery = useDeliveryResource(
    { resource: "scopes", page: 1, pageSize: 25 },
    active && role !== "viewer"
  )
  const backfills =
    query.data?.resource === "backfills"
      ? { ok: true as const, data: query.data.result }
      : null
  const backfillRows = backfills?.data.data ?? null
  const backfillPagination = backfills?.data.pagination ?? null
  const scopes =
    scopesQuery.data?.resource === "scopes" ? scopesQuery.data.result.data : []
  const state = { initialLoading: query.isPending, pending: query.isFetching }
  const create = useServerFn(createHistoricalBackfill)
  const cancel = useServerFn(cancelHistoricalBackfill)
  const preview = useServerFn(previewHistoricalBackfill)
  const queryClient = useQueryClient()
  const { reportError } = useOperationsRefresh()
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
    queryClient.invalidateQueries({
      queryKey: ["operations", "delivery-resource"],
    })
  const backfillColumns = useMemo<ColumnDef<HistoricalBackfill, unknown>[]>(
    () => [
      ...baseBackfillColumns,
      {
        id: "actions",
        header: "操作",
        meta: { width: 90, align: "right" },
        cell: ({ row }) =>
          row.original.status === "queued" ||
          row.original.status === "running" ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label={`取消 ${row.original.provider}/${row.original.dataset_key} ${row.original.start_date} – ${row.original.end_date} 回補`}
              onClick={() =>
                void cancel({ data: { requestId: row.original.request_id } })
                  .then(() =>
                    queryClient.invalidateQueries({
                      queryKey: ["operations", "delivery-resource"],
                    })
                  )
                  .catch(error => {
                    if (!reportError(error))
                      setActionError("取消回補請求失敗。")
                  })
              }
            >
              取消
            </Button>
          ) : null,
      },
    ],
    [cancel, queryClient, reportError]
  )
  const openDays = previewResult?.days.filter(day => day.valid) ?? []
  const closedDays =
    previewResult?.days.filter(day => day.reason === "market_closed") ?? []
  const previewCanCreate = Boolean(
    previewResult?.scope_valid && previewResult.days.length > 0
  )
  const draftComplete = Object.values(backfillDraft).every(Boolean)
  const datasetsForProvider = (provider: string) =>
    scopes
      .filter(scope => scope.provider === provider)
      .map(scope => scope.dataset_key)
  // A prefilled alert may name a scope the list does not contain yet; keep the
  // selected value as an option so the trigger and form value stay visible.
  const withSelected = (options: string[], selected: string) =>
    [...new Set(selected ? [...options, selected] : options)].sort()
  const providerOptions = withSelected(
    scopes.map(scope => scope.provider),
    backfillDraft.provider
  )
  const datasetOptions = backfillDraft.provider
    ? withSelected(
        datasetsForProvider(backfillDraft.provider),
        backfillDraft.dataset
      )
    : []

  useEffect(() => {
    if (!active || !backfillPagination) return
    const page = Math.min(
      search.bp,
      Math.max(backfillPagination.total_pages, 1)
    )
    if (page !== search.bp) updateSearch({ ...search, bp: page })
  }, [active, backfillPagination, search, updateSearch])

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
  useEffect(() => {
    if (!seed) return
    setBackfillDraft({
      provider: seed.source,
      dataset: seed.dataset_key,
      start: seed.expected_data_date,
      end: seed.expected_data_date,
    })
    invalidatePreview()
    setActionError("")
  }, [seed, invalidatePreview])
  const updateBackfillDraft = (
    field: keyof typeof backfillDraft,
    value: string
  ) => {
    setBackfillDraft(current => ({ ...current, [field]: value }))
    invalidatePreview()
    setActionError("")
  }
  const selectBackfillProvider = (provider: string) => {
    const datasets = datasetsForProvider(provider)
    setBackfillDraft(current => ({
      ...current,
      provider,
      dataset: datasets.includes(current.dataset)
        ? current.dataset
        : datasets.length === 1
          ? (datasets[0] ?? "")
          : "",
    }))
    invalidatePreview()
    setActionError("")
  }
  return (
    <>
      <QueryStatus
        hasData={query.data !== undefined}
        pending={query.isFetching}
        error={query.error}
        updatedAt={query.dataUpdatedAt}
        label="回補紀錄"
        onRetry={() => void query.refetch({ cancelRefetch: false })}
      />
      <QueryStatus
        hasData={scopesQuery.data !== undefined}
        pending={scopesQuery.isFetching}
        error={scopesQuery.error}
        updatedAt={scopesQuery.dataUpdatedAt}
        label="回補範圍"
        onRetry={() => void scopesQuery.refetch({ cancelRefetch: false })}
      />
      {role !== "viewer" && (
        <Panel
          className="mt-5"
          eyebrow="Historical"
          title="供應商歷史回補"
          icon={<Clock3 size={19} />}
          result={{ ok: true, data: null }}
        >
          <form
            className="mb-6 overflow-hidden rounded-xl border border-line bg-surface"
            onSubmit={event => {
              event.preventDefault()
              if (creating) return
              if (!confirmed) {
                setActionError("請先確認同一 provider 會依日期順序執行。")
                return
              }
              const currentKey = [
                backfillDraft.provider,
                backfillDraft.dataset,
                backfillDraft.start,
                backfillDraft.end,
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
                  provider: backfillDraft.provider,
                  datasetKey: backfillDraft.dataset,
                  startDate: backfillDraft.start,
                  endDate: backfillDraft.end,
                  requestKey: crypto.randomUUID(),
                },
              })
                .then(() => {
                  updateSearch(current => ({ ...current, bp: 1 }))
                  return refresh()
                })
                .catch(error => {
                  if (!reportError(error)) setActionError("建立回補請求失敗。")
                })
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
              {backfillDraft.provider === "factset" && (
                <p className="m-0 text-sm text-muted">
                  依申請時已發布的港交所現行清單回補；不包含申請時已下市且不在清單中的股票。
                  後續清單更新不會改變這次回補的成員。
                </p>
              )}
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div className="grid gap-1.5">
                  <Label htmlFor="backfill-provider">供應商</Label>
                  <Select
                    required
                    name="provider"
                    value={backfillDraft.provider}
                    disabled={providerOptions.length === 0}
                    onValueChange={selectBackfillProvider}
                  >
                    <SelectTrigger id="backfill-provider">
                      <SelectValue
                        placeholder={
                          scopesQuery.isPending
                            ? "正在載入供應商…"
                            : "選擇供應商"
                        }
                      />
                    </SelectTrigger>
                    <SelectContent>
                      {providerOptions.map(provider => (
                        <SelectItem key={provider} value={provider}>
                          {provider}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor="backfill-dataset">資料集</Label>
                  <Select
                    required
                    name="dataset"
                    value={backfillDraft.dataset}
                    disabled={datasetOptions.length === 0}
                    onValueChange={value =>
                      updateBackfillDraft("dataset", value)
                    }
                  >
                    <SelectTrigger id="backfill-dataset">
                      <SelectValue
                        placeholder={
                          backfillDraft.provider
                            ? "選擇資料集"
                            : "請先選擇供應商"
                        }
                      />
                    </SelectTrigger>
                    <SelectContent>
                      {datasetOptions.map(dataset => (
                        <SelectItem key={dataset} value={dataset}>
                          {dataset}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
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
                  onClick={() => {
                    const input = {
                      provider: backfillDraft.provider,
                      datasetKey: backfillDraft.dataset,
                      startDate: backfillDraft.start,
                      endDate: backfillDraft.end,
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
                      .catch(error => {
                        if (reportError(error)) return
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
                    {previewCanCreate
                      ? "範圍有效；抓取與略過日期會在建立時凍結。"
                      : `範圍無效：${formatBackfillScopeReason(previewResult.scope_reason)}`}
                  </AlertTitle>
                  <AlertDescription>
                    可建立 {openDays.length} 個抓取日期工作項目；略過{" "}
                    {closedDays.length} 個依設定略過日期。
                    {closedDays.length > 0 && (
                      <>
                        略過日期：
                        {closedDays.map(day => day.trade_date).join("、")}。
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
                    ? `驗證已通過；將建立 ${openDays.length} 個抓取日期工作項目，勾選確認後即可送出原始範圍。`
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
          <DataTable
            ariaLabel="回補請求紀錄"
            columns={backfillColumns}
            data={backfillRows ?? []}
            getRowId={row => row.request_id}
            isLoading={state.initialLoading}
            isRefreshing={state.pending && !state.initialLoading}
            refreshingState={null}
            error={query.data === undefined ? query.error : undefined}
            errorState={null}
            emptyState="尚無歷史回補請求。"
            renderExpandedRow={({ original }) => (
              <BackfillItemsTable items={original.items} />
            )}
            fillAvailableWidth
            manualPagination
            pagination={historicalPagination}
            pageCount={Math.max(backfillPagination?.total_pages ?? 1, 1)}
            rowCount={backfillPagination?.total_records ?? 0}
            pageSizeOptions={[25, 50, 100]}
            paginationAriaLabel="歷史回補請求分頁"
            onPaginationChange={next => {
              const value =
                typeof next === "function" ? next(historicalPagination) : next
              updateSearch({
                ...search,
                bp: value.pageSize === search.bps ? value.pageIndex + 1 : 1,
                bps: value.pageSize as DeliveriesPageSearch["bps"],
              })
            }}
          />
        </Panel>
      )}
    </>
  )
}
