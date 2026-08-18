import type { ColumnDef, PaginationState } from "@tanstack/react-table"
import { Archive } from "lucide-react"
import { useEffect, useMemo } from "react"

import { DataTable } from "../../components/data-table"
import type { Correction } from "../../lib/admin-api"
import {
  formatDate,
  OperationsDashboardStatus,
  PageIntro,
  Panel,
  RefreshStatus,
} from "./operations.shared"
import {
  correctionsSearchSchema,
  operationsAuditFromSearch,
  type OperationsPageSearch,
} from "./operations.search"
import {
  useOperationsDashboardQuery,
  useOperationsDashboardState,
} from "./operations.queries"

const columns: ColumnDef<Correction, unknown>[] = [
  {
    accessorKey: "created_at",
    header: "時間",
    meta: { minWidth: 190, pin: "left" },
    cell: context => formatDate(context.getValue<string>()),
  },
  {
    accessorKey: "table_name",
    header: "資料表",
    meta: { minWidth: 180 },
    cell: context => (
      <span className="font-mono wrap-anywhere">
        {context.getValue<string>()}
      </span>
    ),
  },
  {
    accessorKey: "corrected_by",
    header: "修正者",
    meta: { minWidth: 140 },
  },
  {
    accessorKey: "trade_date",
    header: "交易日",
    meta: { minWidth: 120 },
    cell: context => context.getValue<string | null>() ?? "—",
  },
  {
    accessorKey: "correction_reason",
    header: "原因",
    meta: { minWidth: 280, wrap: true },
    cell: context => (
      <span className="whitespace-normal wrap-anywhere">
        {context.getValue<string>()}
      </span>
    ),
  },
]

export function CorrectionsPage({
  search = correctionsSearchSchema.parse({}),
  updateSearch = () => undefined,
}: {
  search?: OperationsPageSearch
  updateSearch?: (next: OperationsPageSearch) => void
} = {}) {
  const audit = operationsAuditFromSearch(search)
  const query = useOperationsDashboardQuery("corrections", audit)
  const state = useOperationsDashboardState(query)
  const correctionsResult =
    state.response?.view === "corrections" ? state.response.corrections : null
  const corrections = correctionsResult?.ok ? correctionsResult.data : null
  const tableColumns = useMemo(() => columns, [])

  useEffect(() => {
    if (!corrections) return
    const totalPages = Math.max(corrections.pagination.total_pages, 1)
    const page = Math.min(Math.max(search.p, 1), totalPages)
    if (page !== search.p) updateSearch({ ...search, p: page })
  }, [corrections, search, updateSearch])

  const pagination: PaginationState = {
    pageIndex: Math.max(search.p - 1, 0),
    pageSize: search.ps,
  }

  return (
    <>
      <PageIntro
        eyebrow="Audit trail"
        title="修正稽核"
        description="檢視 canonical 資料近期的人工修正紀錄。"
      />
      <OperationsDashboardStatus state={state} />
      <Panel
        eyebrow="Recent corrections"
        title="近期修正"
        icon={<Archive size={19} />}
        result={correctionsResult}
        loading={state.initialLoading}
      >
        {corrections ? (
          <>
            <RefreshStatus
              pending={state.pending}
              label="正在更新修正稽核…目前資料仍可使用。"
            />
            <DataTable
              ariaLabel="近期修正"
              caption="近期修正"
              columns={tableColumns}
              data={corrections.data}
              emptyState="目前沒有修正紀錄。"
              fillAvailableWidth
              getRowId={row => row.id}
              isRefreshing={state.pending && !state.initialLoading}
              manualPagination
              pageCount={Math.max(corrections.pagination.total_pages, 1)}
              pagination={pagination}
              pageSizeOptions={[25, 50, 100]}
              rowCount={corrections.pagination.total_records}
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
