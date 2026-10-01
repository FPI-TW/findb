import { useQuery } from "@tanstack/react-query"
import { useState } from "react"

import { Alert, AlertDescription } from "../../components/ui/alert"
import { Button } from "../../components/ui/button"
import { Input } from "../../components/ui/input"
import { Label } from "../../components/ui/label"
import { Skeleton } from "../../components/ui/skeleton"
import { loadFuturesEod } from "./data"
import type { FuturesEodFilters } from "./types"

const sessionLabel = { regular: "日盤", after_hours: "夜盤" } as const
const display = (value: string | number | null) => value ?? "—"

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
          <select
            id="future-session"
            className="h-9 w-full rounded-lg border border-line bg-surface px-3 text-sm"
            value={filters.session}
            onChange={event =>
              update({
                session: event.target.value as FuturesEodFilters["session"],
              })
            }
          >
            <option value="">全部時段</option>
            <option value="regular">日盤</option>
            <option value="after_hours">夜盤</option>
          </select>
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
          <div className="overflow-x-auto rounded-lg border border-line">
            <table className="w-full min-w-[900px] text-left text-xs">
              <caption className="sr-only">
                {productCode} 各合約分時段每日行情
              </caption>
              <thead className="bg-surface-soft">
                <tr>
                  {[
                    "商品",
                    "合約",
                    "契約月份",
                    "交易日",
                    "時段",
                    "開",
                    "高",
                    "低",
                    "收",
                    "成交量",
                    "結算價",
                    "未平倉量",
                  ].map(label => (
                    <th key={label} className="px-2 py-2 font-semibold">
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {query.data.data.map(row => (
                  <tr
                    key={`${row.contract_id}-${row.trade_date}-${row.session}`}
                    className="border-t border-line"
                  >
                    <td className="px-2 py-2 font-mono">{row.product_code}</td>
                    <td className="px-2 py-2 font-mono">{row.contract_code}</td>
                    <td className="px-2 py-2 font-mono">
                      {row.contract_month}
                    </td>
                    <td className="px-2 py-2">{row.trade_date}</td>
                    <td className="px-2 py-2">
                      {sessionLabel[row.session] ?? row.session}
                    </td>
                    <td className="px-2 py-2">{display(row.open)}</td>
                    <td className="px-2 py-2">{display(row.high)}</td>
                    <td className="px-2 py-2">{display(row.low)}</td>
                    <td className="px-2 py-2">{display(row.close)}</td>
                    <td className="px-2 py-2">{display(row.volume)}</td>
                    <td className="px-2 py-2">
                      {display(row.settlement_price)}
                    </td>
                    <td className="px-2 py-2">{display(row.open_interest)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {query.data.data.length === 0 && (
            <p className="text-sm text-muted">此條件沒有期貨行情。</p>
          )}
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
