import { useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { Clipboard, LineChart, RefreshCw, X } from "lucide-react"
import { useEffect, useRef } from "react"

import { DataTable } from "../../components/data-table"
import { Alert, AlertDescription } from "../../components/ui/alert"
import { Button } from "../../components/ui/button"
import { Skeleton } from "../../components/ui/skeleton"
import { loadEod, loadMinute } from "./data"
import type { EodRow, Instrument, MinuteRow } from "./types"

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      "button, [href], input, select, textarea, [tabindex]"
    )
  ).filter(element => {
    if (
      element.tabIndex < 0 ||
      element.matches(":disabled") ||
      (element instanceof HTMLInputElement && element.type === "hidden") ||
      element.closest('[hidden], [aria-hidden="true"]')
    ) {
      return false
    }
    for (
      let current: HTMLElement | null = element;
      current && current !== container.parentElement;
      current = current.parentElement
    ) {
      const style = window.getComputedStyle(current)
      if (style.display === "none" || style.visibility === "hidden")
        return false
    }
    return true
  })
}

function LoadingFeed() {
  return (
    <div className="space-y-2" role="status" aria-live="polite">
      <span className="sr-only">正在載入明細</span>
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-4/5" />
      <Skeleton className="h-4 w-3/5" />
    </div>
  )
}

function FeedError({ message, retry }: { message: string; retry: () => void }) {
  return (
    <Alert variant="destructive">
      <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
        <span>{message}</span>
        <Button type="button" size="sm" variant="outline" onClick={retry}>
          <RefreshCw />
          重試
        </Button>
      </AlertDescription>
    </Alert>
  )
}

const EOD_COLUMNS: ColumnDef<EodRow, unknown>[] = [
  {
    accessorKey: "trade_date",
    header: "交易日",
    cell: ({ row }) => row.original.trade_date,
  },
  {
    accessorKey: "close",
    header: "收盤價",
    cell: ({ row }) => row.original.close ?? "—",
  },
  {
    accessorKey: "volume",
    header: "成交量",
    cell: ({ row }) => row.original.volume ?? "—",
  },
]

const MINUTE_COLUMNS: ColumnDef<MinuteRow, unknown>[] = [
  {
    accessorKey: "bar_start_time",
    header: "Bar time (UTC)",
    cell: ({ row }) => row.original.bar_start_time,
  },
  {
    accessorKey: "close",
    header: "收盤價",
    cell: ({ row }) => row.original.close,
  },
  {
    accessorKey: "volume",
    header: "成交量",
    cell: ({ row }) => row.original.volume ?? "—",
  },
]

function Feed<T>({
  label,
  data,
  columns,
  isLoading,
  isRefreshing,
  error,
  retry,
}: {
  label: string
  data: T[]
  columns: ColumnDef<T, unknown>[]
  isLoading: boolean
  isRefreshing: boolean
  error: unknown
  retry: () => void
}) {
  return (
    <DataTable
      ariaLabel={label}
      caption={label}
      columns={columns}
      data={data}
      emptyState={`目前沒有${label}。`}
      error={error}
      errorState={
        <FeedError
          message={error instanceof Error ? error.message : `無法載入${label}`}
          retry={retry}
        />
      }
      getRowId={(_row, index) => `${label}-${index}`}
      isLoading={isLoading}
      isRefreshing={isRefreshing}
      loadingState={<LoadingFeed />}
      tableClassName="min-w-[420px]"
    />
  )
}

export function DetailDrawer({
  item,
  onClose,
  onCopied,
}: {
  item: Instrument | null
  onClose: () => void
  onCopied: (message: string) => void
}) {
  const drawerRef = useRef<HTMLElement>(null)
  const priorFocusRef = useRef<HTMLElement | null>(null)
  const id = item?.instrument_id ?? ""
  const hasEod = Boolean(item?.coverage.eod)
  const hasMinute = Boolean(item?.coverage.minute)
  const eodQuery = useQuery({
    queryKey: ["lookup-detail", "eod", id],
    queryFn: ({ signal }) => loadEod(id, signal),
    enabled: Boolean(id && hasEod),
  })
  const minuteQuery = useQuery({
    queryKey: ["lookup-detail", "minute", id],
    queryFn: ({ signal }) => loadMinute(id, signal),
    enabled: Boolean(id && hasMinute),
  })

  useEffect(() => {
    if (!item) return
    priorFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
    window.requestAnimationFrame(() => drawerRef.current?.focus())
    return () => priorFocusRef.current?.focus()
  }, [item])

  if (!item) return null

  async function copy(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value)
      onCopied(`${label}已複製`)
    } catch {
      onCopied("無法存取剪貼簿")
    }
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault()
      onClose()
      return
    }
    if (event.key !== "Tab") return
    const drawer = drawerRef.current
    if (!drawer) return
    const focusable = focusableElements(drawer)
    if (!focusable.length) {
      event.preventDefault()
      drawer.focus()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    const activeIndex = focusable.indexOf(document.activeElement as HTMLElement)
    if (document.activeElement === drawer || activeIndex === -1) {
      event.preventDefault()
      ;(event.shiftKey ? last : first)?.focus()
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last?.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first?.focus()
    }
  }

  const meta = [
    ["市場", item.market],
    ["資產類別", item.asset_class],
    ["幣別", item.currency],
    ["時區", item.timezone],
    ["狀態", item.status],
    ["Instrument ID", item.instrument_id],
  ]

  return (
    <>
      <button
        type="button"
        className="fixed inset-0 z-40 cursor-default bg-black/35 backdrop-blur-xs"
        aria-label="關閉詳情"
        tabIndex={-1}
        onClick={onClose}
      />
      <aside
        ref={drawerRef}
        className="fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col overflow-hidden border-l border-line bg-surface shadow-2xl outline-none"
        role="dialog"
        aria-modal="true"
        aria-labelledby="lookup-detail-title"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="flex items-start justify-between gap-4 border-b border-line p-5">
          <div className="min-w-0">
            <p className="font-mono text-xs tracking-widest text-accent uppercase">
              Instrument
            </p>
            <h2
              id="lookup-detail-title"
              className="mt-1 truncate text-xl font-semibold"
            >
              {item.symbol}
            </h2>
            <p className="mt-1 text-sm text-muted">{item.name ?? ""}</p>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label="關閉詳情"
            onClick={onClose}
          >
            <X />
          </Button>
        </header>
        <div className="flex-1 space-y-6 overflow-y-auto p-5">
          <section aria-labelledby="lookup-meta-heading">
            <h3 id="lookup-meta-heading" className="mb-3 text-sm font-semibold">
              基本資訊
            </h3>
            <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {meta.map(([label, value]) => (
                <div
                  key={label ?? "meta"}
                  className="rounded-lg bg-surface-soft px-3 py-2"
                >
                  <dt className="text-xs text-muted">{label}</dt>
                  <dd className="mt-1 break-all font-mono text-sm">
                    {value ?? "—"}
                  </dd>
                </div>
              ))}
            </dl>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => void copy(item.symbol, "Symbol")}
              >
                <Clipboard />
                複製 Symbol
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => void copy(id, "ID")}
              >
                <Clipboard />
                複製 ID
              </Button>
            </div>
          </section>

          <section aria-labelledby="lookup-eod-heading">
            <h3
              id="lookup-eod-heading"
              className="mb-3 flex items-center gap-2 text-sm font-semibold"
            >
              <LineChart size={16} />
              EOD 最近資料
            </h3>
            {hasEod ? (
              <Feed
                label="EOD 資料"
                data={eodQuery.data ?? []}
                columns={EOD_COLUMNS}
                error={eodQuery.error}
                isLoading={eodQuery.isPending}
                isRefreshing={eodQuery.isFetching && !eodQuery.isPending}
                retry={() => void eodQuery.refetch()}
              />
            ) : (
              <p className="text-sm text-muted">
                此商品沒有 active EOD dataset coverage。
              </p>
            )}
          </section>

          <section aria-labelledby="lookup-minute-heading">
            <h3
              id="lookup-minute-heading"
              className="mb-3 flex items-center gap-2 text-sm font-semibold"
            >
              <LineChart size={16} />
              Minute 最近資料
            </h3>
            {hasMinute ? (
              <Feed
                label="Minute 資料"
                data={minuteQuery.data ?? []}
                columns={MINUTE_COLUMNS}
                error={minuteQuery.error}
                isLoading={minuteQuery.isPending}
                isRefreshing={minuteQuery.isFetching && !minuteQuery.isPending}
                retry={() => void minuteQuery.refetch()}
              />
            ) : (
              <p className="text-sm text-muted">
                此商品沒有 active minute dataset coverage。
              </p>
            )}
          </section>
        </div>
      </aside>
    </>
  )
}
