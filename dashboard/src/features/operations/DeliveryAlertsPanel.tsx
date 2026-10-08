import type { ColumnDef } from "@tanstack/react-table"
import { useEffect, useMemo } from "react"
import { Clock3 } from "lucide-react"
import { DataTable } from "../../components/data-table"
import { QueryStatus } from "../../components/AsyncState"
import { Button } from "../../components/ui/button"
import type { MissingDelivery } from "../../lib/admin-api"
import type { AdminRole } from "../../lib/admin-governance-api"
import { formatDate, Panel } from "./operations.shared"
import { useDeliveryResource } from "./delivery.queries"
import type { DeliveriesPageSearch } from "./operations.search"
import type { DeliveriesSearchUpdate } from "./operations.deliveries"

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

export function DeliveryAlertsPanel({
  search,
  updateSearch,
  role,
  active,
  onBackfill,
}: {
  search: DeliveriesPageSearch
  updateSearch: (next: DeliveriesSearchUpdate) => void
  role: AdminRole
  active: boolean
  onBackfill: (row: MissingDelivery) => void
}) {
  const query = useDeliveryResource(
    { resource: "alerts", page: search.p, pageSize: search.ps },
    active
  )
  const result =
    query.data?.resource === "alerts" ? query.data.result : undefined
  const tableColumns = useMemo(
    () =>
      role === "viewer"
        ? columns
        : [
            ...columns,
            {
              id: "backfill",
              header: "回補",
              meta: { width: 120 },
              cell: ({ row }) => (
                <Button
                  variant="outline"
                  size="sm"
                  type="button"
                  aria-label={`帶入 ${row.original.dataset_key} ${row.original.expected_data_date} 回補參數`}
                  onClick={() => onBackfill(row.original)}
                >
                  帶入回補
                </Button>
              ),
            } satisfies ColumnDef<MissingDelivery, unknown>,
          ],
    [role, onBackfill]
  )
  useEffect(() => {
    if (!active || !result) return
    const page = Math.min(search.p, Math.max(result.pagination.total_pages, 1))
    if (page !== search.p) updateSearch({ ...search, p: page })
  }, [active, result, search, updateSearch])
  return (
    <Panel
      title="未解決的交付缺漏"
      eyebrow="Open alerts"
      icon={<Clock3 size={19} />}
      result={{ ok: true, data: null }}
    >
      <p className="mb-3 text-sm text-muted">
        資料集／日期層級的交付告警；全市場逐成員核對請查看「全市場計畫」。
      </p>
      <QueryStatus
        hasData={query.data !== undefined}
        pending={query.isFetching}
        error={query.error}
        updatedAt={query.dataUpdatedAt}
        onRetry={() => void query.refetch({ cancelRefetch: false })}
        label="缺漏告警"
      />
      <DataTable
        ariaLabel="未解決的交付缺漏"
        columns={tableColumns}
        data={result?.data ?? []}
        getRowId={row => row.alert_id}
        isLoading={query.isPending}
        isRefreshing={query.isFetching && !query.isPending}
        refreshingState={null}
        error={query.data === undefined ? query.error : undefined}
        errorState={null}
        emptyState="目前沒有未解決的交付缺漏。"
        fillAvailableWidth
        manualPagination
        pageCount={Math.max(result?.pagination.total_pages ?? 1, 1)}
        rowCount={result?.pagination.total_records ?? 0}
        pagination={{ pageIndex: search.p - 1, pageSize: search.ps }}
        pageSizeOptions={[25, 50, 100]}
        paginationAriaLabel="交付缺漏分頁"
        onPaginationChange={next => {
          const value =
            typeof next === "function"
              ? next({ pageIndex: search.p - 1, pageSize: search.ps })
              : next
          updateSearch({
            ...search,
            p: value.pageSize === search.ps ? value.pageIndex + 1 : 1,
            ps: value.pageSize,
          })
        }}
      />
    </Panel>
  )
}
