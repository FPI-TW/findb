import { useEffect } from "react"
import { useQuery } from "@tanstack/react-query"
import { useServerFn } from "@tanstack/react-start"
import type { ColumnDef } from "@tanstack/react-table"
import { ClipboardList } from "lucide-react"
import { DataTable } from "../../components/data-table"
import { QueryStatus } from "../../components/AsyncState"
import { useProtectedQueryScope } from "../../components/ProtectedQueryScope"
import {
  liveQueryOptions,
  manualQueryOptions,
  useRegisterOperationsQuery,
} from "../../components/OperationsRefresh"
import { Badge } from "../../components/ui/badge"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import {
  loadDeliveryPlans,
  loadDeliveryPlanDatasets,
} from "../../lib/admin.functions"
import type { DeliveryPlan } from "../../lib/admin-api"
import { formatDate, Panel } from "./operations.shared"
import {
  deliveriesSearchSchema,
  type DeliveriesPageSearch,
} from "./operations.search"
import type { DeliveriesSearchUpdate } from "./operations.deliveries"

const reasonLabels: Record<string, string> = {
  mapping_gap: "商品對照缺漏",
  quota: "額度不足",
  rate_limited: "來源請求頻率受限",
  halted: "暫停交易",
  no_trade: "無成交紀錄",
  pending: "尚待交付",
  permission_denied: "權限不足",
  source_error: "來源錯誤",
}
const columns: ColumnDef<DeliveryPlan, unknown>[] = [
  {
    accessorKey: "dataset_key",
    header: "資料集",
    meta: { minWidth: 180, pin: "left" },
  },
  { accessorKey: "provider", header: "供應商", meta: { width: 110 } },
  { accessorKey: "trade_date", header: "交易日", meta: { width: 110 } },
  {
    accessorKey: "status",
    header: "完整性",
    meta: { width: 90 },
    cell: ({ row }) => (
      <Badge
        variant={row.original.status === "complete" ? "secondary" : "outline"}
      >
        {row.original.status === "complete" ? "完整" : "不完整"}
      </Badge>
    ),
  },
  {
    id: "deadline",
    header: "截止時間",
    meta: { width: 190 },
    cell: ({ row }) => (
      <div>
        {formatDate(row.original.summary.deadline_at)}
        <span className="ml-2 inline-block w-10 text-danger">
          {row.original.summary.is_late ? "已逾時" : ""}
        </span>
      </div>
    ),
  },
  ...(
    [
      ["expected", "預期"],
      ["data", "有資料"],
      ["no_data", "正常無資料"],
      ["missing", "缺漏"],
      ["blocked", "受阻"],
    ] as const
  ).map(([key, header]): ColumnDef<DeliveryPlan, unknown> => ({
    id: key,
    header,
    meta: { width: key === "no_data" ? 110 : 80, align: "right" },
    cell: ({ row }) => (
      <span className="font-mono tabular-nums">
        {row.original.summary[key]}
      </span>
    ),
  })),
]

function PlanDetails({ plan }: { plan: DeliveryPlan }) {
  const { summary } = plan
  return (
    <div className="space-y-3">
      <p className="break-all text-xs">
        Universe release：<span className="font-mono">{plan.release_id}</span>
      </p>
      <p className="text-xs text-muted">
        預期 = 有資料 + 正常無資料 + 缺漏 +
        受阻。正常無資料須有持久化證據，不代表失敗。
      </p>
      <p className="text-sm font-medium">
        待處理缺口：已列出 {summary.gaps.length}／總缺口{" "}
        {summary.missing + summary.blocked}（明細上限 1,000 筆）
      </p>
      {summary.gaps.length ? (
        <ul className="max-h-64 space-y-1 overflow-auto text-xs">
          {summary.gaps.map(gap => (
            <li key={`${gap.member_key}:${gap.status}`}>
              <span className="font-mono">{gap.member_key}</span>：
              {gap.status === "missing"
                ? "缺漏"
                : gap.status === "blocked"
                  ? "受阻"
                  : gap.status}
              {gap.reason && (
                <span>
                  {" "}
                  ·{" "}
                  {reasonLabels[gap.reason]
                    ? `${reasonLabels[gap.reason]}（${gap.reason}）`
                    : gap.reason}
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted">此計畫沒有待處理缺口。</p>
      )}
    </div>
  )
}

export function DeliveryPlansPanel({
  search = deliveriesSearchSchema.parse({}),
  updateSearch = () => {},
  active = true,
}: {
  search?: DeliveriesPageSearch
  updateSearch?: (next: DeliveriesSearchUpdate) => void
  active?: boolean
}) {
  const load = useServerFn(loadDeliveryPlans)
  const loadDatasets = useServerFn(loadDeliveryPlanDatasets)
  const scope = useProtectedQueryScope()
  const profile = search.profile ?? "pilot"
  const label = profile === "pilot" ? "Pilot 交付計畫" : "Full market 交付計畫"
  const request = {
    profile,
    datasetKey: search.dataset,
    tradeDate: search.date,
    page: search.pp,
    pageSize: search.pps,
  }
  const queryKey = ["operations", "delivery-plans", scope, request] as const
  const datasetsKey = [
    "operations",
    "delivery-plan-datasets",
    scope,
    profile,
  ] as const
  useRegisterOperationsQuery(queryKey, active)
  useRegisterOperationsQuery(datasetsKey, active)
  const datasets = useQuery({
    ...manualQueryOptions,
    queryKey: datasetsKey,
    queryFn: () => loadDatasets({ data: { profile } }),
    enabled: active,
  })
  const query = useQuery({
    ...liveQueryOptions,
    queryKey,
    queryFn: () => load({ data: request }),
    enabled: active,
  })
  const pagination = query.data?.pagination
  useEffect(() => {
    if (!active || !pagination) return
    const page = Math.min(search.pp, Math.max(pagination.total_pages, 1))
    if (page !== search.pp) updateSearch({ ...search, pp: page })
  }, [active, pagination, search, updateSearch])
  const options = Array.from(
    new Set([
      ...(datasets.data?.data ?? []),
      ...(search.dataset ? [search.dataset] : []),
    ])
  ).sort()
  return (
    <Panel
      eyebrow={profile === "pilot" ? "Pilot" : "Full market"}
      title={label}
      icon={<ClipboardList size={19} />}
      result={{ ok: true, data: null }}
    >
      <p className="mb-4 text-sm text-muted">
        依固定 universe
        與交易日逐成員核對交付。本區為唯讀監控，不會啟動抓取或建立回補。
      </p>
      {query.data?.execution_policies && (
        <p className="mb-3 text-sm text-muted">
          目前非交易日政策：
          {Object.entries(query.data.execution_policies)
            .map(
              ([provider, policy]) =>
                `${provider}：${policy.skip_non_trading_days ? "略過" : "照常抓取"}`
            )
            .join("；")}
          。設定經部署生效。
        </p>
      )}
      <div className="mb-2 grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor="plan-dataset">資料集</Label>
          <select
            id="plan-dataset"
            className="h-9 w-full rounded-lg border border-line bg-surface px-3 text-sm text-ink outline-none focus-visible:ring-3 focus-visible:ring-accent/20"
            value={search.dataset}
            disabled={datasets.isPending}
            onChange={event =>
              updateSearch({ ...search, dataset: event.target.value, pp: 1 })
            }
          >
            <option value="">
              {datasets.isPending ? "正在載入資料集…" : "全部資料集"}
            </option>
            {options.map(key => (
              <option key={key} value={key}>
                {key}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="plan-date">交易日</Label>
          <Input
            id="plan-date"
            type="date"
            value={search.date}
            onChange={event =>
              updateSearch({ ...search, date: event.target.value, pp: 1 })
            }
          />
        </div>
      </div>
      <QueryStatus
        hasData={datasets.data !== undefined}
        pending={datasets.isFetching}
        error={datasets.error}
        updatedAt={datasets.dataUpdatedAt}
        onRetry={() => void datasets.refetch({ cancelRefetch: false })}
        label="資料集選項"
      />
      <QueryStatus
        hasData={query.data !== undefined}
        pending={query.isFetching}
        error={query.error}
        updatedAt={query.dataUpdatedAt}
        onRetry={() => void query.refetch({ cancelRefetch: false })}
        label={label}
      />
      <DataTable
        ariaLabel={label}
        columns={columns}
        data={query.data?.data ?? []}
        getRowId={plan => plan.plan_id}
        isLoading={query.isPending}
        isRefreshing={query.isFetching && !query.isPending}
        refreshingState={null}
        error={query.data === undefined ? query.error : undefined}
        errorState={null}
        emptyState="此模式在所選條件尚無交付計畫；不代表已完成或已有資料缺漏。"
        renderExpandedRow={({ original }) => <PlanDetails plan={original} />}
        fillAvailableWidth
        manualPagination
        pagination={{ pageIndex: search.pp - 1, pageSize: search.pps }}
        pageCount={Math.max(pagination?.total_pages ?? 1, 1)}
        rowCount={pagination?.total_records ?? 0}
        pageSizeOptions={[25, 50, 100]}
        paginationAriaLabel={`${label}分頁`}
        onPaginationChange={next => {
          const value =
            typeof next === "function"
              ? next({ pageIndex: search.pp - 1, pageSize: search.pps })
              : next
          updateSearch({
            ...search,
            pp: value.pageSize === search.pps ? value.pageIndex + 1 : 1,
            pps: value.pageSize,
          })
        }}
      />
      {query.data?.skipped && (
        <section className="mt-5 space-y-2" aria-label="依設定略過">
          <h3 className="font-medium">
            依設定略過（{query.data.skipped_total} 個日期／資料集）
          </h3>
          <p className="text-sm text-muted">
            不計入預期、缺漏或正常無資料。顯示符合上方篩選的最近 100
            筆；可指定日期查看。
          </p>
          {query.data.skipped.length === 0 ? (
            <p className="text-sm text-muted">沒有符合條件的略過紀錄。</p>
          ) : (
            <ul className="max-h-64 space-y-1 overflow-auto text-sm">
              {query.data.skipped.map(row => (
                <li
                  key={`${row.execution_profile}:${row.provider}:${row.dataset_key}:${row.trade_date}`}
                >
                  {row.trade_date} · {row.provider} · {row.dataset_key} ·{" "}
                  {row.snapshot.calendar_source === "published"
                    ? `已發布日曆 r${row.snapshot.calendar_revision}`
                    : "週一至週五預設"}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </Panel>
  )
}
