import {
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
} from "lucide-react"

import { Button } from "../ui/button"
import { cn } from "#/lib/utils"
import type { DataTablePaginationProps } from "./types"

const DEFAULT_PAGE_SIZE_OPTIONS = [10, 25, 50]

export function DataTablePagination({
  pageIndex,
  pageSize,
  pageCount,
  rowCount,
  pageSizeOptions = DEFAULT_PAGE_SIZE_OPTIONS,
  onPageChange,
  onPageSizeChange,
  className,
}: DataTablePaginationProps) {
  const normalizedPageCount = Math.max(pageCount, 1)
  const canPreviousPage = pageIndex > 0
  const canNextPage = pageIndex < normalizedPageCount - 1
  const normalizedPageSizeOptions = Array.from(
    new Set([...pageSizeOptions, pageSize])
  ).sort((left, right) => left - right)
  const firstRow = rowCount && rowCount > 0 ? pageIndex * pageSize + 1 : 0
  const lastRow =
    rowCount && rowCount > 0
      ? Math.min((pageIndex + 1) * pageSize, rowCount)
      : 0

  return (
    <nav
      aria-label="資料表分頁"
      className={cn(
        "flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3 text-xs text-muted",
        className
      )}
      data-slot="data-table-pagination"
    >
      <div className="flex items-center gap-2">
        {rowCount !== undefined ? (
          <span aria-live="polite">
            {rowCount === 0
              ? "沒有資料"
              : `顯示第 ${firstRow}–${lastRow} 筆，共 ${rowCount} 筆`}
          </span>
        ) : (
          <span aria-live="polite">
            第 {Math.min(pageIndex + 1, normalizedPageCount)} /{" "}
            {normalizedPageCount} 頁
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2">
          <span>每頁</span>
          <select
            aria-label="每頁筆數"
            className="h-8 rounded-md border border-line bg-surface px-2 text-xs text-ink outline-none focus-visible:ring-3 focus-visible:ring-accent/20"
            value={pageSize}
            onChange={event => onPageSizeChange(Number(event.target.value))}
          >
            {normalizedPageSizeOptions.map(option => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <div
          className="flex items-center gap-1"
          role="group"
          aria-label="頁面導覽"
        >
          <Button
            aria-label="第一頁"
            disabled={!canPreviousPage}
            onClick={() => onPageChange(0)}
            size="icon-xs"
            type="button"
            variant="outline"
          >
            <ChevronsLeft aria-hidden="true" />
          </Button>
          <Button
            aria-label="上一頁"
            disabled={!canPreviousPage}
            onClick={() => onPageChange(Math.max(pageIndex - 1, 0))}
            size="icon-xs"
            type="button"
            variant="outline"
          >
            <ChevronLeft aria-hidden="true" />
          </Button>
          <Button
            aria-label="下一頁"
            disabled={!canNextPage}
            onClick={() =>
              onPageChange(Math.min(pageIndex + 1, normalizedPageCount - 1))
            }
            size="icon-xs"
            type="button"
            variant="outline"
          >
            <ChevronRight aria-hidden="true" />
          </Button>
          <Button
            aria-label="最後一頁"
            disabled={!canNextPage}
            onClick={() => onPageChange(normalizedPageCount - 1)}
            size="icon-xs"
            type="button"
            variant="outline"
          >
            <ChevronsRight aria-hidden="true" />
          </Button>
        </div>
      </div>
    </nav>
  )
}
