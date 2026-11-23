import {
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
} from "lucide-react"

import { Button } from "../ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select"
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
  ariaLabel,
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
  const navigationLabel = ariaLabel ?? "資料表分頁"
  const controlLabel = (label: string) =>
    ariaLabel ? `${ariaLabel}${label}` : label

  return (
    <nav
      aria-label={navigationLabel}
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
        <div className="flex items-center gap-2">
          <span aria-hidden="true">每頁</span>
          <Select
            value={String(pageSize)}
            onValueChange={value => onPageSizeChange(Number(value))}
          >
            <SelectTrigger
              aria-label={controlLabel("每頁筆數")}
              size="xs"
              className="w-auto"
            >
              <SelectValue>{pageSize}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              {normalizedPageSizeOptions.map(option => (
                <SelectItem key={option} value={String(option)}>
                  {option}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div
          className="flex items-center gap-1"
          role="group"
          aria-label={controlLabel("頁面導覽")}
        >
          <Button
            aria-label={controlLabel("第一頁")}
            disabled={!canPreviousPage}
            onClick={() => onPageChange(0)}
            size="icon-xs"
            type="button"
            variant="outline"
          >
            <ChevronsLeft aria-hidden="true" />
          </Button>
          <Button
            aria-label={controlLabel("上一頁")}
            disabled={!canPreviousPage}
            onClick={() => onPageChange(Math.max(pageIndex - 1, 0))}
            size="icon-xs"
            type="button"
            variant="outline"
          >
            <ChevronLeft aria-hidden="true" />
          </Button>
          <Button
            aria-label={controlLabel("下一頁")}
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
            aria-label={controlLabel("最後一頁")}
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
