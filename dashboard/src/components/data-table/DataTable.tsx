import * as React from "react"
import {
  flexRender,
  functionalUpdate,
  getCoreRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type Column,
  type ColumnDef,
  type ExpandedState,
  type Header,
  type OnChangeFn,
  type Row,
  type RowData,
  type SortingState,
} from "@tanstack/react-table"

import { cn } from "#/lib/utils"
import { DataTablePagination } from "./DataTablePagination"
import type {
  DataTableCellStyle,
  DataTableColumnMeta,
  DataTableProps,
} from "./types"

declare module "@tanstack/react-table" {
  // Consumers can use `meta: { width, align, pin }` directly in ColumnDef.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<
    TData extends RowData,
    TValue,
  > extends DataTableColumnMeta {}
}

const DEFAULT_PAGE_SIZE = 25
const EXPANDER_COLUMN_WIDTH = 40

function stableValue(value: unknown): string {
  if (value === null) return "null"
  if (typeof value === "string") return JSON.stringify(value)
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value)
  }
  if (Array.isArray(value)) {
    return `[${value.map(item => stableValue(item)).join(",")}]`
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(
      ([left], [right]) => left.localeCompare(right)
    )
    return `{${entries
      .map(([key, item]) => `${JSON.stringify(key)}:${stableValue(item)}`)
      .join(",")}}`
  }
  return String(value)
}

function defaultGetRowId<TData>(originalRow: TData): string {
  if (
    typeof originalRow === "object" &&
    originalRow !== null &&
    !Array.isArray(originalRow)
  ) {
    const record = originalRow as Record<string, unknown>
    for (const key of ["id", "key", "uuid", "_id"]) {
      const value = record[key]
      if (
        typeof value === "string" ||
        typeof value === "number" ||
        typeof value === "bigint"
      ) {
        return String(value)
      }
    }
  }
  return `row:${stableValue(originalRow)}`
}

function cssLength(value: number | string): string {
  return typeof value === "number" ? `${value}px` : value
}

function columnWidth<TData extends RowData>(column: Column<TData, unknown>) {
  return (
    column.columnDef.meta?.width ??
    column.columnDef.meta?.minWidth ??
    column.getSize()
  )
}

function addCssLengths(values: Array<number | string>): number | string {
  if (values.length === 0) return 0
  if (values.every(value => typeof value === "number")) {
    return values.reduce<number>((total, value) => total + value, 0)
  }
  return `calc(${values.map(cssLength).join(" + ")})`
}

function pinnedOffset<TData extends RowData>(
  column: Column<TData, unknown>,
  columns: Column<TData, unknown>[],
  side: "left" | "right",
  includeExpander: boolean
): number | string {
  const pinnedColumns = columns.filter(
    candidate => candidate.columnDef.meta?.pin === side
  )
  const columnIndex = pinnedColumns.indexOf(column)
  if (columnIndex < 0) return 0

  const precedingColumns =
    side === "left"
      ? pinnedColumns.slice(0, columnIndex)
      : pinnedColumns.slice(columnIndex + 1)
  const lengths = precedingColumns.map(candidate => columnWidth(candidate))
  if (side === "left" && includeExpander) {
    lengths.unshift(EXPANDER_COLUMN_WIDTH)
  }
  return addCssLengths(lengths)
}

function cellStyle<TData extends RowData>(
  column: Column<TData, unknown>,
  columns: Column<TData, unknown>[],
  includeExpander: boolean
): DataTableCellStyle {
  const meta = column.columnDef.meta
  const style: DataTableCellStyle = {}
  const width = columnWidth(column)
  style.width = cssLength(width)
  if (meta?.minWidth !== undefined) style.minWidth = cssLength(meta.minWidth)
  else if (meta?.width !== undefined) style.minWidth = cssLength(meta.width)

  if (meta?.pin) {
    style.position = "sticky"
    style[meta.pin] = pinnedOffset(column, columns, meta.pin, includeExpander)
  }
  return style
}

function cellAlignment(meta: DataTableColumnMeta | undefined): string {
  if (meta?.align === "center") return "text-center"
  if (meta?.align === "right") return "text-right"
  return "text-left"
}

function cellWrapping(meta: DataTableColumnMeta | undefined): string {
  return meta?.wrap ? "whitespace-normal break-words" : "whitespace-nowrap"
}

function pinClasses(
  meta: DataTableColumnMeta | undefined,
  header: boolean
): string {
  if (!meta?.pin) return ""
  return cn(
    header ? "z-30 bg-surface-soft" : "z-10 bg-surface",
    meta.pin === "left" ? "border-r border-line" : "border-l border-line"
  )
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return "資料載入失敗，請稍後再試。"
}

function sortingLabel<TData extends RowData>(
  column: Column<TData, unknown>,
  sorted: false | "asc" | "desc"
) {
  if (sorted === "asc") return "目前依此欄位升冪排序"
  if (sorted === "desc") return "目前依此欄位降冪排序"
  return `依${String(column.columnDef.header ?? "此欄位")}排序`
}

function HeaderCell<TData extends RowData>({
  header,
  columns,
  includeExpander,
}: {
  header: Header<TData, unknown>
  columns: Column<TData, unknown>[]
  includeExpander: boolean
}) {
  const sorted = header.column.getIsSorted()
  const canSort = header.column.getCanSort()
  const meta = header.column.columnDef.meta
  const ariaSort =
    sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : "none"

  return (
    <th
      aria-sort={ariaSort}
      className={cn(
        "sticky top-0 h-10 border-b border-line bg-surface-soft px-3 text-xs font-bold tracking-wide text-muted uppercase",
        cellAlignment(meta),
        cellWrapping(meta),
        pinClasses(meta, true)
      )}
      colSpan={header.colSpan}
      data-column-id={header.column.id}
      key={header.id}
      scope="col"
      style={cellStyle(header.column, columns, includeExpander)}
    >
      {header.isPlaceholder ? null : canSort ? (
        <button
          aria-label={sortingLabel(header.column, sorted)}
          className="inline-flex w-full cursor-pointer items-center gap-1 text-inherit outline-none focus-visible:ring-3 focus-visible:ring-accent/20"
          onClick={header.column.getToggleSortingHandler()}
          type="button"
        >
          <span className="min-w-0 flex-1">
            {flexRender(header.column.columnDef.header, header.getContext())}
          </span>
          <span
            aria-hidden="true"
            className="shrink-0 font-mono text-[0.65rem] text-accent"
          >
            {sorted === "asc" ? "↑" : sorted === "desc" ? "↓" : "↕"}
          </span>
        </button>
      ) : (
        flexRender(header.column.columnDef.header, header.getContext())
      )}
    </th>
  )
}

function ExpandButton<TData extends RowData>({ row }: { row: Row<TData> }) {
  return (
    <button
      aria-label={row.getIsExpanded() ? "收合詳細資料" : "展開詳細資料"}
      aria-expanded={row.getIsExpanded()}
      className="inline-flex size-6 cursor-pointer items-center justify-center rounded-md text-muted outline-none hover:bg-accent-soft hover:text-accent focus-visible:ring-3 focus-visible:ring-accent/20"
      onClick={() => row.toggleExpanded()}
      type="button"
    >
      <span aria-hidden="true" className="font-mono text-sm leading-none">
        {row.getIsExpanded() ? "−" : "+"}
      </span>
    </button>
  )
}

function DataTableStateRow({
  children,
  colSpan,
  role,
}: {
  children: React.ReactNode
  colSpan: number
  role?: "alert" | "status"
}) {
  return (
    <tr>
      <td
        className="px-4 py-12 text-center text-sm text-muted"
        colSpan={colSpan}
      >
        <div aria-live={role === "status" ? "polite" : undefined} role={role}>
          {children}
        </div>
      </td>
    </tr>
  )
}

export function DataTable<TData extends RowData>({
  columns,
  data,
  caption,
  ariaLabel,
  getRowId,
  sorting: controlledSorting,
  onSortingChange,
  pagination: controlledPagination,
  onPaginationChange,
  pageCount,
  rowCount,
  enablePagination,
  manualPagination,
  manualSorting = false,
  pageSizeOptions,
  isLoading = false,
  isRefreshing = false,
  error,
  loadingState,
  refreshingState,
  errorState,
  emptyState,
  renderExpandedRow,
  getRowCanExpand,
  expanded: controlledExpanded,
  onExpandedChange,
  className,
  viewportClassName,
  tableClassName,
  fillAvailableWidth = false,
  "aria-busy": ariaBusy,
}: DataTableProps<TData>) {
  const [internalSorting, setInternalSorting] = React.useState<SortingState>([])
  const [internalPagination, setInternalPagination] = React.useState({
    pageIndex: 0,
    pageSize: DEFAULT_PAGE_SIZE,
  })
  const [internalExpanded, setInternalExpanded] = React.useState<ExpandedState>(
    {}
  )

  const sorting = controlledSorting ?? internalSorting
  const pagination = controlledPagination ?? internalPagination
  const expanded = controlledExpanded ?? internalExpanded
  const paginationEnabled =
    (enablePagination ?? controlledPagination !== undefined) ||
    pageCount !== undefined ||
    onPaginationChange !== undefined
  const isManualPagination = manualPagination ?? pageCount !== undefined

  const updateSorting: OnChangeFn<SortingState> = updater => {
    const next = functionalUpdate(updater, sorting)
    if (controlledSorting === undefined) setInternalSorting(next)
    onSortingChange?.(next)
  }
  const updatePagination: OnChangeFn<typeof pagination> = updater => {
    const next = functionalUpdate(updater, pagination)
    if (controlledPagination === undefined) setInternalPagination(next)
    onPaginationChange?.(next)
  }
  const updateExpanded: OnChangeFn<ExpandedState> = updater => {
    const next = functionalUpdate(updater, expanded)
    if (controlledExpanded === undefined) setInternalExpanded(next)
    onExpandedChange?.(next)
  }

  const tableOptions = {
    data,
    columns: columns as ColumnDef<TData, unknown>[],
    defaultColumn: { enableSorting: false },
    state: { sorting, pagination, expanded },
    getRowId: getRowId
      ? (originalRow: TData, index: number) => getRowId(originalRow, index)
      : (originalRow: TData) => defaultGetRowId(originalRow),
    onSortingChange: updateSorting,
    onPaginationChange: updatePagination,
    onExpandedChange: updateExpanded,
    enableExpanding: renderExpandedRow !== undefined,
    getRowCanExpand: (row: Row<TData>) =>
      renderExpandedRow !== undefined &&
      (getRowCanExpand?.(row.original) ?? true),
    manualPagination: isManualPagination,
    manualSorting,
    getCoreRowModel: getCoreRowModel(),
    ...(paginationEnabled
      ? { getPaginationRowModel: getPaginationRowModel() }
      : {}),
    ...(manualSorting ? {} : { getSortedRowModel: getSortedRowModel() }),
    ...(pageCount !== undefined ? { pageCount } : {}),
  }
  const table = useReactTable<TData>(tableOptions)
  const leafColumns = table.getVisibleLeafColumns()
  const rows = table.getRowModel().rows
  const includeExpander = renderExpandedRow !== undefined
  const totalColumnCount = Math.max(
    leafColumns.length + (includeExpander ? 1 : 0),
    1
  )
  const shownRowCount =
    rowCount ?? (isManualPagination ? undefined : data.length)
  const resolvedPageCount = paginationEnabled ? table.getPageCount() : 0

  return (
    <div className={cn("w-full", className)} data-slot="data-table">
      {isRefreshing ? (
        <p
          aria-live="polite"
          className="mb-2 text-xs font-medium text-muted"
          role="status"
        >
          {refreshingState ?? "正在更新資料…"}
        </p>
      ) : null}

      <div
        aria-busy={(ariaBusy ?? isLoading) || isRefreshing}
        className={cn(
          "relative max-h-128 w-full overflow-auto rounded-lg border border-line",
          viewportClassName
        )}
        data-slot="data-table-viewport"
      >
        <table
          aria-label={ariaLabel}
          aria-busy={(ariaBusy ?? isLoading) || isRefreshing}
          className={cn(
            "border-separate border-spacing-0 text-xs",
            fillAvailableWidth ? "w-full min-w-max" : "w-max min-w-full",
            tableClassName
          )}
          data-slot="data-table-table"
        >
          <caption className="sr-only">
            {caption ?? ariaLabel ?? "資料表"}
          </caption>
          <thead>
            {table.getHeaderGroups().map(headerGroup => (
              <tr key={headerGroup.id}>
                {includeExpander ? (
                  <th
                    aria-label="展開"
                    className="sticky top-0 z-30 w-10 min-w-10 border-b border-line bg-surface-soft px-2"
                    scope="col"
                    style={{
                      left: 0,
                      position: "sticky",
                      width: `${EXPANDER_COLUMN_WIDTH}px`,
                    }}
                  />
                ) : null}
                {headerGroup.headers.map(header => (
                  <HeaderCell
                    columns={leafColumns}
                    header={header}
                    includeExpander={includeExpander}
                    key={header.id}
                  />
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {isLoading ? (
              <DataTableStateRow colSpan={totalColumnCount} role="status">
                {loadingState ?? "正在載入資料…"}
              </DataTableStateRow>
            ) : error ? (
              <DataTableStateRow colSpan={totalColumnCount} role="alert">
                {errorState ?? errorMessage(error)}
              </DataTableStateRow>
            ) : rows.length === 0 ? (
              <DataTableStateRow colSpan={totalColumnCount}>
                {emptyState ?? "目前沒有資料。"}
              </DataTableStateRow>
            ) : (
              rows.map(row => (
                <React.Fragment key={row.id}>
                  <tr
                    aria-expanded={
                      includeExpander ? row.getIsExpanded() : undefined
                    }
                    className="border-b border-line transition-colors last:border-0 hover:bg-surface-soft"
                    data-row-id={row.id}
                    data-state={row.getIsExpanded() ? "expanded" : undefined}
                  >
                    {includeExpander ? (
                      <td
                        className="sticky left-0 z-10 w-10 min-w-10 border-r border-line bg-surface px-2 py-2 align-top"
                        style={{
                          left: 0,
                          position: "sticky",
                          width: `${EXPANDER_COLUMN_WIDTH}px`,
                        }}
                      >
                        {row.getCanExpand() ? <ExpandButton row={row} /> : null}
                      </td>
                    ) : null}
                    {row.getVisibleCells().map(cell => {
                      const meta = cell.column.columnDef.meta
                      return (
                        <td
                          className={cn(
                            "border-b border-line px-3 py-2 align-top text-ink",
                            cellAlignment(meta),
                            cellWrapping(meta),
                            pinClasses(meta, false)
                          )}
                          data-column-id={cell.column.id}
                          key={cell.id}
                          style={cellStyle(
                            cell.column,
                            leafColumns,
                            includeExpander
                          )}
                        >
                          {flexRender(
                            cell.column.columnDef.cell,
                            cell.getContext()
                          )}
                        </td>
                      )
                    })}
                  </tr>
                  {includeExpander && row.getIsExpanded() ? (
                    <tr
                      className="border-b border-line bg-surface-soft"
                      data-expanded-row="true"
                    >
                      <td
                        className="px-4 py-3 text-sm text-ink"
                        colSpan={totalColumnCount}
                      >
                        {renderExpandedRow?.(row)}
                      </td>
                    </tr>
                  ) : null}
                </React.Fragment>
              ))
            )}
          </tbody>
        </table>
      </div>

      {paginationEnabled ? (
        <DataTablePagination
          className="mt-3"
          onPageChange={pageIndex =>
            updatePagination({ ...pagination, pageIndex })
          }
          onPageSizeChange={pageSize =>
            updatePagination({ pageIndex: 0, pageSize })
          }
          pageCount={resolvedPageCount}
          pageIndex={pagination.pageIndex}
          pageSize={pagination.pageSize}
          pageSizeOptions={pageSizeOptions}
          rowCount={shownRowCount}
        />
      ) : null}
    </div>
  )
}
