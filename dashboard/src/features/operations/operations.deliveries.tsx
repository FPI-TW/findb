import type { ColumnDef, PaginationState } from "@tanstack/react-table"
import { Clock3 } from "lucide-react"
import { useEffect, useMemo } from "react"

import { DataTable } from "../../components/data-table"
import type { MissingDelivery } from "../../lib/admin-api"
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
}: {
  search?: OperationsPageSearch
  updateSearch?: (next: OperationsPageSearch) => void
} = {}) {
  const audit = operationsAuditFromSearch(search)
  const query = useOperationsDashboardQuery("deliveries", audit)
  const state = useOperationsDashboardState(query)
  const deliveries =
    state.response?.view === "deliveries" ? state.response.deliveries : null
  const rows = deliveries?.ok ? deliveries.data : null

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
    </>
  )
}
