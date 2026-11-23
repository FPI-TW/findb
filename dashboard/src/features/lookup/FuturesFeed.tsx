import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { useState } from "react"

import { DataTable } from "../../components/data-table"
import { Alert, AlertDescription } from "../../components/ui/alert"
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
import { Skeleton } from "../../components/ui/skeleton"
import { loadFuturesEod } from "./data"
import type { FuturesEodFilters, FuturesEodRow } from "./types"

const sessionLabel = { regular: "日盤", after_hours: "夜盤" } as const
const display = (value: string | number | null) => value ?? "—"

const monoCell = (value: string | number | null) => (
  <span className="font-mono">{display(value)}</span>
)

const futuresColumns: ColumnDef<FuturesEodRow, unknown>[] = [
  {
    accessorKey: "product_code",
    header: "商品",
    cell: context => monoCell(context.getValue<string>()),
  },
  {
    accessorKey: "contract_code",
    header: "合約",
    cell: context => monoCell(context.getValue<string>()),
  },
  {
    accessorKey: "contract_month",
    header: "契約月份",
    cell: context => monoCell(context.getValue<string>()),
  },
  { accessorKey: "trade_date", header: "交易日" },
  {
    accessorKey: "session",
    header: "時段",
    cell: context => {
      const session = context.getValue<FuturesEodRow["session"]>()
      return sessionLabel[session] ?? session
    },
  },
  ...(
    [
      ["open", "開"],
      ["high", "高"],
      ["low", "低"],
      ["close", "收"],
      ["volume", "成交量"],
      ["settlement_price", "結算價"],
      ["open_interest", "未平倉量"],
    ] as const
  ).map(([key, header]): ColumnDef<FuturesEodRow, unknown> => ({
    accessorKey: key,
    header,
    meta: { align: "right" },
    cell: context => display(context.getValue<string | number | null>()),
  })),
]

export function FuturesFeed({ productCode }: { productCode: string }) {
  const [filters, setFilters] = useState<FuturesEodFilters>({
    productCode,
    contractCode: "",
    startDate: "",
    endDate: "",
    session: "",
    cursor: "",
  })
  const [cursorHistory, setCursorHistory] = useState<string[]>([])
  const query = useQuery({
    queryKey: ["lookup-detail", "futures-eod", filters],
    queryFn: ({ signal }) => loadFuturesEod(filters, signal),
  })
  const update = (patch: Partial<FuturesEodFilters>) => {
    setFilters(current => ({ ...current, ...patch, cursor: "" }))
    setCursorHistory([])
  }

  return (
    <section aria-labelledby="lookup-futures-heading" className="space-y-3">
      <h3 id="lookup-futures-heading" className="text-sm font-semibold">
        期貨合約每日行情
      </h3>
      <p className="text-xs text-muted">
        每筆資料對應單一合約與交易時段。日盤與夜盤分別呈現。
      </p>
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <Label htmlFor="future-contract">合約代碼</Label>
          <Input
            id="future-contract"
            value={filters.contractCode}
            onChange={event => update({ contractCode: event.target.value })}
            placeholder="例如 TX:202610 或 TX:202610W1"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="future-session">交易時段</Label>
          <Select
            value={filters.session || "all"}
            onValueChange={value =>
              update({
                session: (value === "all"
                  ? ""
                  : value) as FuturesEodFilters["session"],
              })
            }
          >
            <SelectTrigger id="future-session" size="sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部時段</SelectItem>
              <SelectItem value="regular">日盤</SelectItem>
              <SelectItem value="after_hours">夜盤</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="future-start">起始交易日</Label>
          <Input
            id="future-start"
            type="date"
            value={filters.startDate}
            onChange={event => update({ startDate: event.target.value })}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="future-end">結束交易日</Label>
          <Input
            id="future-end"
            type="date"
            value={filters.endDate}
            onChange={event => update({ endDate: event.target.value })}
          />
        </div>
      </div>
      {query.isPending ? (
        <div role="status" aria-live="polite" className="space-y-2">
          <span className="sr-only">正在載入期貨行情</span>
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
        </div>
      ) : query.isError ? (
        <Alert variant="destructive">
          <AlertDescription>
            {query.error.message}
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void query.refetch()}
            >
              重試
            </Button>
          </AlertDescription>
        </Alert>
      ) : (
        <>
          {query.isFetching && (
            <p role="status" className="text-xs text-muted">
              正在更新期貨行情…
            </p>
          )}
          <DataTable
            caption={`${productCode} 各合約分時段每日行情`}
            columns={futuresColumns}
            data={query.data.data}
            getRowId={row =>
              `${row.contract_id}-${row.trade_date}-${row.session}`
            }
            refreshingState={null}
            emptyState="此條件沒有期貨行情。"
            fillAvailableWidth
          />
          <div className="flex items-center justify-end gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={cursorHistory.length === 0}
              onClick={() => {
                const previous = cursorHistory.at(-1) ?? ""
                setCursorHistory(current => current.slice(0, -1))
                setFilters(current => ({ ...current, cursor: previous }))
              }}
            >
              上一頁
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={!query.data.pagination.next_cursor}
              onClick={() => {
                const next = query.data.pagination.next_cursor
                if (!next) return
                setCursorHistory(current => [...current, filters.cursor])
                setFilters(current => ({ ...current, cursor: next }))
              }}
            >
              下一頁
            </Button>
          </div>
        </>
      )}
    </section>
  )
}
