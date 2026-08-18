import type { CSSProperties, ReactNode } from "react"

import type {
  ColumnDef,
  ExpandedState,
  OnChangeFn,
  PaginationState,
  Row,
  RowData,
  SortingState,
} from "@tanstack/react-table"

/** Metadata used by DataTable to render a column consistently. */
export type DataTableColumnMeta = {
  /** Fixed column width. Numbers are interpreted as pixels. */
  width?: number | string
  /** Minimum column width. Numbers are interpreted as pixels. */
  minWidth?: number | string
  /** Horizontal alignment for headers and cells. */
  align?: "left" | "center" | "right"
  /** Allow cell contents to wrap instead of staying on one line. */
  wrap?: boolean
  /** Keep this column visible while the table viewport scrolls horizontally. */
  pin?: "left" | "right"
}

export type DataTableState = {
  pagination: PaginationState
  sorting: SortingState
  expanded: ExpandedState
}

export type DataTablePaginationProps = {
  pageIndex: number
  pageSize: number
  pageCount: number
  rowCount?: number | undefined
  pageSizeOptions?: number[] | undefined
  onPageChange: (pageIndex: number) => void
  onPageSizeChange: (pageSize: number) => void
  className?: string
}

export type DataTableProps<TData extends RowData> = {
  /** Column definitions from @tanstack/react-table. */
  columns: ColumnDef<TData, unknown>[]
  /** The rows currently available to the table. */
  data: TData[]
  /** Accessible name for the table caption. */
  caption?: ReactNode
  /** Optional aria-label when a caption is not suitable for the surrounding UI. */
  ariaLabel?: string
  /** Stable identifier for each row. Prefer a domain key over the fallback. */
  getRowId?: (originalRow: TData, index: number) => string
  /** Controlled sorting state. Omit to use the table's local state. */
  sorting?: SortingState
  onSortingChange?: OnChangeFn<SortingState>
  /** Controlled pagination state. Omit to use the table's local state. */
  pagination?: PaginationState
  onPaginationChange?: OnChangeFn<PaginationState>
  /** Total number of pages for a server-paginated table. */
  pageCount?: number
  /** Total rows across all pages, used for the pagination summary. */
  rowCount?: number
  /** Show pagination controls even when no controlled pagination state is supplied. */
  enablePagination?: boolean
  /** Treat data and pageCount as server-paginated values. */
  manualPagination?: boolean
  /** Treat sorting as server-side and only emit sorting callbacks. */
  manualSorting?: boolean
  pageSizeOptions?: number[]
  /** Render a row-level loading state before the first successful response. */
  isLoading?: boolean
  /** Keep existing rows visible while showing a small refresh status. */
  isRefreshing?: boolean
  /** Error value; a truthy value renders the error state. */
  error?: unknown
  loadingState?: ReactNode
  refreshingState?: ReactNode
  errorState?: ReactNode
  emptyState?: ReactNode
  /** Render an expandable detail row for each row. */
  renderExpandedRow?: (row: Row<TData>) => ReactNode
  getRowCanExpand?: (originalRow: TData) => boolean
  expanded?: ExpandedState
  onExpandedChange?: OnChangeFn<ExpandedState>
  /** Extra classes for the outer table root. */
  className?: string
  /** Extra classes for the single horizontally/vertically scrolling viewport. */
  viewportClassName?: string
  /** A class applied to the table element itself. */
  tableClassName?: string
  "aria-busy"?: boolean
}

export type DataTableCellStyle = CSSProperties & {
  left?: string | number
  right?: string | number
}
